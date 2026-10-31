"""Comparative offline regression for task selection, ranking and fallback.

All account authorization in eligible scenarios is a pytest-only monkeypatch.
No provider inference, discovery, probes, billing access or network I/O.
"""
from dataclasses import replace

import pytest

from router import Router, RouteRequest
from router.catalog import CapabilityEvidence, ModelSpec, ProviderCatalog, ProviderSpec
from router.registry import RegisteredProvider
from router.selector import ineligibility_reason, model_coverage, rank
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing


def _model(name, *, caps=("chat",), price="free", cost=5, unsupported=()):
    evidence = tuple(
        CapabilityEvidence(cap, "verified", "synthetic_offline", "2026-10-28")
        for cap in caps
    ) + tuple(
        CapabilityEvidence(cap, "unsupported", "synthetic_offline", "2026-10-28")
        for cap in unsupported
    )
    return ModelSpec(
        id=name,
        capabilities=frozenset(caps),
        model_class="standard",
        strategic_cost=cost,
        billing_status=price,
        evidence=evidence,
    )


def _spec(provider_id, *models):
    endpoints = {
        "groq": "https://api.groq.com/openai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    return ProviderSpec(
        id=provider_id,
        aliases=(provider_id,),
        base_url=endpoints[provider_id],
        models=tuple(models),
        strategic_cost=10,
        priority=50,
        discover_models=False,
    )


def _registered(spec):
    return RegisteredProvider(spec, "OFFLINE_FAKE_KEY", RuntimeState())


@pytest.fixture
def hypothetical_account_approval(monkeypatch):
    # A test-only scenario, never evidence that a real account is free.
    monkeypatch.setattr(
        "router.billing.account_billing_ineligibility_reason", lambda _: None
    )


def test_equal_scores_have_stable_model_and_provider_tiebreaks(hypothetical_account_approval):
    groq = _registered(_spec("groq", _model("fixture-z"), _model("fixture-a")))
    openrouter = _registered(_spec("openrouter", _model("fixture-b")))
    request = RouteRequest("hola", decompose=False)
    left = [(d.provider, d.model) for d in rank([openrouter, groq], request)]
    right = [(d.provider, d.model) for d in rank([groq, openrouter], request)]
    assert left == right == [
        ("groq", "fixture-a"),
        ("groq", "fixture-z"),
        ("openrouter", "fixture-b"),
    ]


def test_code_task_prefers_code_but_generic_chat_conserves_specialist(
    hypothetical_account_approval,
):
    provider = _registered(_spec(
        "groq",
        _model("fixture-general"),
        _model("fixture-code", caps=("chat", "code")),
    ))
    coding = rank([provider], RouteRequest("Generá un script en Python", decompose=False))
    generic = rank([provider], RouteRequest("Hola, cómo estás", decompose=False))
    assert [item.model for item in coding] == ["fixture-code", "fixture-general"]
    assert [item.model for item in generic] == ["fixture-general", "fixture-code"]
    assert any("advisory_task_bonus=" in r for r in coding[0].reasons)


def test_image_input_requires_vision_even_if_task_looks_like_plain_chat(
    hypothetical_account_approval,
):
    provider = _registered(_spec(
        "groq",
        _model("fixture-text"),
        _model("fixture-vision", caps=("chat", "vision")),
        _model("fixture-conflict", caps=("chat", "vision"), unsupported=("vision",)),
    ))
    request = RouteRequest(
        "hola", images=("data:image/png;base64,ZmFrZQ==",), decompose=False
    )
    assert [(d.provider, d.model) for d in rank([provider], request)] == [
        ("groq", "fixture-vision")
    ]
    assert ineligibility_reason(provider, provider.spec.models[0], request) == "missing_capability"
    assert ineligibility_reason(provider, provider.spec.models[2], request) == "unsupported_capability"


def test_130_model_inventory_excludes_unknown_and_paid_without_losing_code_preference(
    hypothetical_account_approval,
):
    models = [_model(f"fixture-{i:03d}") for i in range(127)]
    models += [
        _model("fixture-code", caps=("chat", "code")),
        _model("fixture-paid", price="paid"),
        _model("fixture-unknown", price="unknown"),
    ]
    provider = _registered(_spec("groq", *models))
    request = RouteRequest("Generá un script en Python", decompose=False)
    decisions = rank([provider], request)
    coverage = model_coverage([provider], request)
    assert len(models) == 130
    assert len(decisions) == 128
    assert decisions[0].model == "fixture-code"
    assert "fixture-paid" not in {d.model for d in decisions}
    assert "fixture-unknown" not in {d.model for d in decisions}
    assert coverage["totals"]["cataloged_models"] == 130
    assert coverage["totals"]["eligible_models"] == 128
    assert coverage["providers"][0]["excluded_by_reason"] == {
        "billing_paid": 1,
        "billing_unverified": 1,
    }


def test_fallback_uses_next_ranked_model_and_records_both_outcomes(
    hypothetical_account_approval,
):
    class OfflineClient:
        def __init__(self):
            self.calls = []

        def complete(self, provider, model, request):
            self.calls.append((provider.spec.id, model))
            if model == "fixture-code":
                raise RuntimeError("synthetic_model_rejection")
            return "fixture response", {"fixture": True}

    client = OfflineClient()
    spec = _spec(
        "groq",
        _model("fixture-general"),
        _model("fixture-code", caps=("chat", "code")),
    )
    router = Router(client=client, catalog=ProviderCatalog((spec,)), max_retries=0)
    provider = router.add_provider("groq", "OFFLINE_FAKE_KEY", discover=False)
    response = router.route(RouteRequest("Generá un script en Python", decompose=False))
    assert response.ok is True
    assert response.model == "fixture-general"
    assert client.calls == [
        ("groq", "fixture-code"),
        ("groq", "fixture-general"),
    ]
    assert [(a.model, a.ok) for a in response.attempts] == [
        ("fixture-code", False),
        ("fixture-general", True),
    ]
    assert provider.state.model("fixture-code").failure_count == 1
    assert provider.state.model("fixture-general").success_count == 1


def test_unknown_price_stays_blocked_without_account_audit():
    provider = _registered(_spec("groq", _model("fixture-unknown", price="unknown")))
    request = RouteRequest("hola", decompose=False)
    assert rank([provider], request) == []
    assert ineligibility_reason(provider, provider.spec.models[0], request) == "billing_unverified"
