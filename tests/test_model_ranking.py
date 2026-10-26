"""Stage 5: real selector preview, with zero network and no billing bypass."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.ranking_diagnostics import ranking_report
from router.ranking_evidence import verification_tier
from router.registry import RegisteredProvider
from router.selector import ineligibility_reason
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


def _model(model_id, evidence=()):
    return ModelSpec(
        model_id, frozenset({"chat", "code", "coding"}), "standard",
        priority=50, strategic_cost=20, evidence=tuple(evidence),
        billing_status="free",
    )


def _provider(provider_id, models):
    spec = next(item for item in BUILTINS if item.id == provider_id)
    return RegisteredProvider(
        replace(spec, models=tuple(models)), "fixture-key-not-for-network", RuntimeState()
    )


def _record(cap, status, source="offline-fixture", checked="2026-10-26T18:00:00Z"):
    return CapabilityEvidence(cap, status, source, checked)


def test_legacy_verified_is_not_a_verified_ranking_bonus():
    legacy = _model("vendor/legacy", [_record("chat", "verified", "migrated_from_v1")])
    fresh = _model("vendor/fresh", [_record("chat", "verified")])
    assert verification_tier(legacy, frozenset({"chat"})) == 1
    assert verification_tier(fresh, frozenset({"chat"})) == 0


def test_conflicting_alias_evidence_blocks_selection_even_if_declared():
    model = _model("vendor/conflict", [
        _record("code", "verified"), _record("coding", "unsupported"),
    ])
    provider = _provider("groq", [model])
    assert verification_tier(model, frozenset({"code"})) == 9
    assert ineligibility_reason(
        provider, model, RouteRequest("write code", required_capabilities=frozenset({"code"}))
    ) == "unsupported_capability"


def test_inconclusive_is_not_accepted_as_verified():
    model = _model("vendor/inconclusive", [_record("chat", "inconclusive")])
    assert verification_tier(model, frozenset({"chat"})) == 2


def test_billing_pending_excludes_models_and_does_not_invent_scores():
    provider = _provider("groq", [_model("vendor/unapproved")])
    report = ranking_report([provider], "hello")
    assert report["cataloged_models"] == 1
    assert report["ranked_models"] == 0
    assert report["ranking"] == []
    assert report["providers"][0]["excluded_models"] == 1
    assert report["account_billing_verified"] is False
    assert report["quality_measured"] is False


def test_all_models_compete_individually_without_four_option_cap(monkeypatch):
    import router.selector as selector
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    models = [_model(f"vendor/model-{i:02d}") for i in range(12)]
    providers = [_provider("groq", models), _provider("openrouter", [_model("vendor/extra")])]
    report = ranking_report(providers, "hello", limit=5)
    assert report["ranked_models"] == 13
    assert len(report["ranking"]) == 5
    assert report["preview_truncated"] is True
    full = ranking_report(providers, "hello", limit=100)
    assert len(full["ranking"]) == 13
    assert len({(row["provider"], row["model"]) for row in full["ranking"]}) == 13
    assert all("smoothed_reliability=" in " ".join(row["reasons"]) for row in full["ranking"])


def test_observed_reliability_and_latency_rank_models_not_provider_only(monkeypatch):
    import router.selector as selector
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider("groq", [_model("vendor/slow-failing"), _model("vendor/fast-reliable")])
    bad = provider.state.model("vendor/slow-failing")
    bad.failure_count, bad.ewma_latency_ms = 10, 6000.0
    good = provider.state.model("vendor/fast-reliable")
    good.success_count, good.ewma_latency_ms = 10, 150.0
    report = ranking_report([provider], "hello")
    assert [item["model"] for item in report["ranking"]] == [
        "vendor/fast-reliable", "vendor/slow-failing",
    ]
    assert report["ranking"][0]["observed_attempts"] == 10
    assert report["ranking"][0]["observed_success_rate"] == 1.0
    assert report["ranking"][1]["observed_success_rate"] == 0.0
    assert all(item["quality_score"] is None for item in report["ranking"])


def test_unknown_or_unparsed_explicit_requirements_fail_closed():
    provider = _provider("groq", [_model("vendor/a")])
    with pytest.raises(ValueError, match="invalid_explicit_requirements"):
        ranking_report([provider], "hello", requirements=("imaginary-capability",))
    with pytest.raises(ValueError, match="invalid_explicit_requirements"):
        ranking_report([provider], "hello", required_capabilities=frozenset({"fake"}))


def test_task_hints_are_not_promoted_to_hard_requirements(monkeypatch):
    import router.selector as selector
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider("groq", [_model("vendor/a")])
    report = ranking_report([provider], "Escribí código en Python", required_capabilities=frozenset({"chat"}))
    assert report["ranked_models"] == 1
    assert report["task_interpretation"]["explicit_required_capabilities"] == ["chat"]
    assert "code" in {
        item["capability"] for item in report["task_interpretation"]["suggested_capabilities"]
    }


def test_admin_endpoint_is_offline_and_redacts_task_and_keys(monkeypatch):
    import router.api as api

    provider = _provider("groq", [_model("vendor/a")])
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "stage5-admin")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "stage5-app")

    class NoNetwork:
        def complete(self, *args, **kwargs):
            pytest.fail("unexpected inference")
        def probe_capability(self, *args, **kwargs):
            pytest.fail("unexpected probe")
        def discover_inventory(self, *args, **kwargs):
            pytest.fail("unexpected discovery")

    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider]), client=NoNetwork()
    ))
    client = TestClient(api.app)
    url = "/diagnostics/model-ranking"
    payload = {"task": "private task text", "required_capabilities": ["chat"]}
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={
        "Authorization": "Bearer stage5-app",
    }).status_code == 401
    response = client.post(url, json=payload, headers={
        "Authorization": "Bearer stage5-admin",
    })
    assert response.status_code == 200
    assert response.json()["inference_performed"] is False
    assert response.json()["ranked_models"] == 0
    assert "private task text" not in response.text
    assert "fixture-key-not-for-network" not in response.text
    assert "stage5-admin" not in response.text
    invalid = client.post(url, json={
        "task": "hello", "requirements": ["invalid-thing"],
    }, headers={"Authorization": "Bearer stage5-admin"})
    assert invalid.status_code == 422
