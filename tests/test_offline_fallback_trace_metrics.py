"""Offline fallback and request-trace regressions.

Only a scripted in-memory client is used. Test-only billing monkeypatching is
not account verification and never enables inference in deployed Router.
"""
import httpx
import pytest

from router import Router, RouteRequest
from router.catalog import CapabilityEvidence, ModelSpec, ProviderCatalog, ProviderSpec
from router.storage import RouterStore

pytestmark = pytest.mark.strict_billing


@pytest.fixture
def hypothetical_account_approval(monkeypatch):
    monkeypatch.setattr(
        "router.billing.account_billing_ineligibility_reason", lambda _: None
    )


def _model(name, *, priority=50, cost=5):
    return ModelSpec(
        id=name,
        capabilities=frozenset({"chat"}),
        model_class="standard",
        priority=priority,
        strategic_cost=cost,
        billing_status="free",
        evidence=(CapabilityEvidence("chat", "verified", "synthetic_offline", "2026-10-28"),),
    )


def _provider(name, *models, priority=50):
    endpoints = {
        "groq": "https://api.groq.com/openai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    return ProviderSpec(
        id=name,
        aliases=(name,),
        base_url=endpoints[name],
        models=tuple(models),
        priority=priority,
        strategic_cost=10,
        discover_models=False,
    )


class ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = {key: list(values) for key, values in outcomes.items()}
        self.calls = []

    def complete(self, provider, model, request):
        key = (provider.spec.id, model)
        self.calls.append(key)
        outcome = self.outcomes[key].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"offline": True}


def _router(tmp_path, client, *providers, max_retries=0):
    store = RouterStore(tmp_path / "offline-fallback.db")
    router = Router(
        client=client,
        catalog=ProviderCatalog(tuple(providers)),
        store=store,
        max_retries=max_retries,
    )
    for provider in providers:
        router.add_provider(provider.id, "OFFLINE_FAKE_KEY", discover=False)
    return router, store


def _http_error(status, *, retry_after=None):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("synthetic status", request=request, response=response)


def test_same_provider_model_fallback_is_visible_in_trace(tmp_path, hypothetical_account_approval):
    client = ScriptedClient({
        ("groq", "primary"): [RuntimeError("synthetic model error")],
        ("groq", "secondary"): ["recovered"],
    })
    router, store = _router(
        tmp_path, client,
        _provider("groq", _model("primary", priority=90), _model("secondary", priority=20)),
    )
    response = router.route(RouteRequest("hola", decompose=False, application_name="offline"))
    assert response.ok and response.model == "secondary"
    assert client.calls == [("groq", "primary"), ("groq", "secondary")]
    trace = store.recent_request_traces(application_name="offline")[0]
    assert trace["fallback"] is True
    assert trace["model_fallback"] is True
    assert trace["provider_fallback"] is False
    assert [(a["model"], a["success"]) for a in trace["attempts"]] == [
        ("primary", False), ("secondary", True)
    ]


def test_retry_of_same_model_is_not_counted_as_fallback(tmp_path, hypothetical_account_approval):
    client = ScriptedClient({
        ("groq", "only"): [httpx.ReadTimeout("synthetic timeout"), "recovered"],
    })
    router, store = _router(
        tmp_path, client, _provider("groq", _model("only")), max_retries=1,
    )
    response = router.route(RouteRequest("hola", decompose=False))
    assert response.ok and len(response.attempts) == 2
    trace = store.recent_request_traces()[0]
    assert trace["fallback"] is False
    assert trace["provider_fallback"] is False
    assert trace["model_fallback"] is False
    assert [a["attempt_number"] for a in trace["attempts"]] == [1, 2]
    stats = store.recent_model_stats()[("groq", "only")]
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 1


def test_provider_wide_429_skips_sibling_models_and_uses_other_provider(
    tmp_path, hypothetical_account_approval,
):
    client = ScriptedClient({
        ("groq", "first"): [_http_error(429, retry_after="3600")],
        ("openrouter", "backup"): ["recovered"],
    })
    router, store = _router(
        tmp_path, client,
        _provider("groq", _model("first", priority=90), _model("never", priority=10), priority=90),
        _provider("openrouter", _model("backup"), priority=10),
        max_retries=3,
    )
    response = router.route(RouteRequest("hola", decompose=False))
    assert response.ok and response.provider == "openrouter"
    assert client.calls == [("groq", "first"), ("openrouter", "backup")]
    assert router.registry.all()[0].state.available is False
    trace = store.recent_request_traces()[0]
    assert trace["fallback"] is True
    assert trace["provider_fallback"] is True
    assert trace["model_fallback"] is False
    assert [(a["error_type"], a["error_code"], a["success"]) for a in trace["attempts"]] == [
        ("quota", 429, False), (None, None, True)
    ]


def test_strict_cost_ceiling_is_not_raised_after_failure(
    tmp_path, hypothetical_account_approval,
):
    client = ScriptedClient({
        ("openrouter", "cheap"): [RuntimeError("synthetic failure")],
    })
    router, store = _router(
        tmp_path, client,
        _provider("groq", _model("expensive", cost=16), priority=90),
        _provider("openrouter", _model("cheap", cost=5), priority=10),
    )
    response = router.route(RouteRequest("hola", decompose=False, max_strategic_cost=15))
    assert not response.ok and response.error == "all_attempts_failed"
    assert client.calls == [("openrouter", "cheap")]
    assert [(d.provider, d.model) for d in response.decisions] == [("openrouter", "cheap")]
    assert store.recent_request_traces()[0]["fallback"] is False


def test_mixed_model_failures_and_cross_provider_fallback_keep_metrics(
    tmp_path, hypothetical_account_approval,
):
    client = ScriptedClient({
        ("groq", "first"): [RuntimeError("synthetic rejection")],
        ("groq", "second"): [httpx.ReadTimeout("synthetic timeout")],
        ("openrouter", "third"): ["recovered"],
    })
    router, store = _router(
        tmp_path, client,
        _provider("groq", _model("first", priority=90), _model("second", priority=20), priority=90),
        _provider("openrouter", _model("third"), priority=10),
    )
    response = router.route(RouteRequest("hola", decompose=False))
    assert response.ok and response.provider == "openrouter"
    assert client.calls == [
        ("groq", "first"), ("groq", "second"), ("openrouter", "third")
    ]
    trace = store.recent_request_traces()[0]
    assert trace["fallback"] and trace["model_fallback"] and trace["provider_fallback"]
    assert [(a["provider"], a["error_type"], a["success"]) for a in trace["attempts"]] == [
        ("groq", "provider_error", False),
        ("groq", "timeout", False),
        ("openrouter", None, True),
    ]
    assert [a["attempt_number"] for a in trace["attempts"]] == [1, 2, 3]
    assert router.registry.all()[0].state.failure_count == 2
    assert router.registry.all()[1].state.success_count == 1
    stats = store.recent_model_stats()
    assert stats[("groq", "first")]["failure_count"] == 1
    assert stats[("groq", "second")]["failure_count"] == 1
    assert stats[("openrouter", "third")]["success_count"] == 1
