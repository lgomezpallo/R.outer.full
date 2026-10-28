"""Offline cooldown recovery and crash/restart regressions.

Only scripted clients and temporary SQLite are used; no provider API or probe.
The billing monkeypatch is test-scoped and is NOT live account authorization.
"""
import httpx
import pytest

from router import Router, RouteRequest
from router.catalog import CapabilityEvidence, ModelSpec, ProviderCatalog, ProviderSpec
from router.selector import ineligibility_reason
from router.storage import RouterStore

pytestmark = pytest.mark.strict_billing


@pytest.fixture(autouse=True)
def synthetic_billing_approval(monkeypatch):
    monkeypatch.setattr(
        "router.billing.account_billing_ineligibility_reason", lambda _: None
    )


def _model(name, priority=50):
    return ModelSpec(
        id=name,
        capabilities=frozenset({"chat"}),
        model_class="standard",
        priority=priority,
        strategic_cost=5,
        billing_status="free",
        evidence=(CapabilityEvidence("chat", "verified", "offline_fixture", "2026-10-28"),),
    )


def _provider(*models):
    return ProviderSpec(
        id="groq",
        aliases=("groq",),
        base_url="https://api.groq.com/openai/v1",
        models=tuple(models),
        priority=50,
        strategic_cost=10,
        discover_models=False,
    )


class ScriptedClient:
    def __init__(self, outcomes):
        self.outcomes = {name: list(values) for name, values in outcomes.items()}
        self.calls = []

    def complete(self, provider, model, request):
        self.calls.append((provider.spec.id, model))
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome, {"synthetic": True}


def _router(store, provider, client, *, retries=0):
    router = Router(
        client=client,
        catalog=ProviderCatalog((provider,)),
        store=store,
        max_retries=retries,
    )
    registered = router.add_provider("groq", "SYNTHETIC_KEY", discover=False)
    return router, registered


def _status_error(status, retry_after):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, request=request, headers={"Retry-After": str(retry_after)})
    return httpx.HTTPStatusError("synthetic failure", request=request, response=response)


def test_retry_failure_is_durable_even_if_process_stops_before_retry(tmp_path):
    store = RouterStore(tmp_path / "restart.db")
    provider = _provider(_model("only"))
    interrupted = ScriptedClient({
        "only": [httpx.ReadTimeout("offline timeout"), SystemExit("synthetic interruption")]
    })
    router, first = _router(store, provider, interrupted, retries=1)
    with pytest.raises(SystemExit, match="synthetic interruption"):
        router.route(RouteRequest("hola", decompose=False))
    assert first.state.failure_count == 1
    assert store.load_provider_runtime("groq")["failure_count"] == 1
    assert store.recent_model_stats()[("groq", "only")]["failure_count"] == 1

    restarted, hydrated = _router(store, provider, ScriptedClient({"only": ["ok"]}))
    assert hydrated.state.failure_count == 1
    assert hydrated.state.model("only").failure_count == 1
    assert restarted.route(RouteRequest("hola", decompose=False)).ok
    assert store.load_provider_runtime("groq")["success_count"] == 1
    assert store.load_provider_runtime("groq")["failure_count"] == 1
    stats = store.recent_model_stats()[("groq", "only")]
    assert (stats["failure_count"], stats["success_count"]) == (1, 1)


def test_provider_429_cooldown_survives_restart_then_recovers(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr("router.state.time", lambda: clock[0])
    store = RouterStore(tmp_path / "cooldown.db")
    provider = _provider(_model("only"))
    first_client = ScriptedClient({"only": [_status_error(429, 3600)]})
    router, registered = _router(store, provider, first_client, retries=3)
    first = router.route(RouteRequest("hola", decompose=False))
    assert not first.ok and len(first_client.calls) == 1
    deadline = registered.state.cooldown_until
    assert deadline >= clock[0] + 3600
    assert store.load_provider_runtime("groq")["cooldown_until"] == deadline

    second_client = ScriptedClient({"only": ["recovered"]})
    restarted, hydrated = _router(store, provider, second_client)
    assert hydrated.state.cooldown_until == deadline
    assert not restarted.route(RouteRequest("hola", decompose=False)).ok
    assert second_client.calls == []

    clock[0] = deadline + 1
    result = restarted.route(RouteRequest("hola", decompose=False))
    assert result.ok and result.model == "only"
    assert second_client.calls == [("groq", "only")]
    assert store.load_provider_runtime("groq")["cooldown_until"] == 0.0
    assert store.load_provider_runtime("groq")["cooldown_strikes"] == 0
    traces = store.recent_request_traces()
    assert len(traces) == 2
    assert [len(trace["attempts"]) for trace in traces] == [1, 1]
    assert traces[0]["attempts"][0]["success"] is True
    assert traces[1]["attempts"][0]["error_code"] == 429


def test_model_503_cooldown_survives_restart_without_blocking_sibling(tmp_path, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr("router.state.time", lambda: clock[0])
    store = RouterStore(tmp_path / "model-cooldown.db")
    provider = _provider(_model("first", priority=95), _model("backup", priority=5))
    client = ScriptedClient({
        "first": [_status_error(503, 300)],
        "backup": ["recovered"],
    })
    router, registered = _router(store, provider, client, retries=3)
    result = router.route(RouteRequest("hola", decompose=False))
    assert result.ok and result.model == "backup"
    assert client.calls == [("groq", "first"), ("groq", "backup")]
    assert registered.state.available
    deadline = registered.state.model("first").cooldown_until
    assert deadline >= clock[0] + 300
    assert store.load_model_runtime("groq")["first"]["cooldown_until"] == deadline

    restarted, hydrated = _router(store, provider, ScriptedClient({"backup": ["ok"]}))
    request = RouteRequest("hola", decompose=False)
    first_model = next(model for model in hydrated.spec.models if model.id == "first")
    backup_model = next(model for model in hydrated.spec.models if model.id == "backup")
    assert ineligibility_reason(hydrated, first_model, request) == "model_cooldown"
    assert ineligibility_reason(hydrated, backup_model, request) is None
    assert restarted.route(request).model == "backup"
    clock[0] = deadline + 1
    assert ineligibility_reason(hydrated, first_model, request) is None
