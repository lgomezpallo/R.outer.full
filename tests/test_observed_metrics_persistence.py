"""Stage 7: persisted routing observations, never provider inference.

All fixtures are SQLite writes and injected clients. Probe outcomes and HTTP
failure duration must not masquerade as successful model performance.
"""
from time import time

import httpx
import pytest

from router.router import Router
from router.state import RuntimeState
from router.storage import RouterStore
from router.types import Decision, RouteRequest


def _metric(store, *, request_id, phase="direct", success=True, latency=100, model="openai/gpt-oss-20b"):
    store.record_metric(
        request_id=request_id, application_name="offline-test",
        provider_id="groq", model_id=model, phase=phase,
        attempt_number=1, latency_ms=latency, success=success,
        error_type=None if success else "timeout",
    )


def test_probe_observations_never_train_routing_reliability_or_performance(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _metric(store, request_id="p1", phase="probe:chat", success=True, latency=1)
    _metric(store, request_id="p2", phase="probe:json", success=False, latency=9999)
    _metric(store, request_id="r1", phase="direct", success=False, latency=4000)
    _metric(store, request_id="r2", phase="plan", success=True, latency=200)
    _metric(store, request_id="r3", phase="subtask:execute", success=True, latency=300)
    stats = store.recent_model_stats()
    item = stats[("groq", "openai/gpt-oss-20b")]
    assert item["samples"] == 3
    assert (item["success_count"], item["failure_count"]) == (2, 1)
    assert item["success_rate"] == pytest.approx(2 / 3)
    assert item["latency_ms"] == pytest.approx(230)
    report = store.model_performance_report()
    assert len(report) == 1
    assert report[0]["attempts"] == 3
    assert report[0]["successful_requests"] == 2
    assert report[0]["errors"] == {"timeout": 1}


def test_restart_replays_success_only_latency_ewma_in_chronological_order(tmp_path):
    path = tmp_path / "router.db"
    store = RouterStore(path)
    _metric(store, request_id="r1", success=True, latency=100)
    _metric(store, request_id="r2", success=True, latency=200)
    _metric(store, request_id="r3", success=False, latency=9000)
    _metric(store, request_id="r4", success=True, latency=300)
    # The latency should be 100 -> 130 -> 181, not the arithmetic mean
    # and not influenced by the timeout duration.
    assert store.recent_model_stats()[("groq", "openai/gpt-oss-20b")]["latency_ms"] == pytest.approx(181)
    router = Router(store=RouterStore(path))
    provider = router.add_provider("groq", "offline-key", discover=False)
    state = provider.state.model("openai/gpt-oss-20b")
    assert (state.success_count, state.failure_count) == (3, 1)
    assert state.ewma_latency_ms == pytest.approx(181)


def test_all_failed_attempts_leave_latency_unknown_after_restart(tmp_path):
    path = tmp_path / "router.db"
    store = RouterStore(path)
    _metric(store, request_id="r1", success=False, latency=5000)
    _metric(store, request_id="r2", success=False, latency=6000)
    _metric(store, request_id="p1", phase="probe:chat", success=True, latency=1)
    router = Router(store=RouterStore(path))
    state = router.add_provider("groq", "offline-key", discover=False).state.model("openai/gpt-oss-20b")
    assert (state.success_count, state.failure_count) == (0, 2)
    assert state.ewma_latency_ms is None


def test_provider_and_model_cooldowns_persist_across_restart(tmp_path):
    path = tmp_path / "router.db"
    store = RouterStore(path)
    state = RuntimeState()
    state.mark_failure("openai/gpt-oss-20b", "offline 503", 1800, failure_kind="server", provider_wide=False)
    store.save_provider_runtime("groq", state)
    store.save_model_runtime("groq", "openai/gpt-oss-20b", state.model("openai/gpt-oss-20b"))
    reloaded = Router(store=RouterStore(path)).add_provider("groq", "offline-key", discover=False)
    assert reloaded.state.model("openai/gpt-oss-20b").cooldown_until > time() + 1700
    assert reloaded.state.model("openai/gpt-oss-20b").last_error == "offline 503"


class OfflineRetryClient:
    def __init__(self):
        self.calls = 0

    def complete(self, provider, model, req):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout(
                "simulated timeout",
                request=httpx.Request("POST", "https://example.invalid/offline"),
            )
        return "simulated success", {"offline": True}


def test_retry_failure_count_matches_persisted_attempts(monkeypatch, tmp_path):
    # Billing is bypassed only for the injected client that never uses network.
    # Production's billing gate is covered by independent fail-closed tests.
    import router.router as router_module

    monkeypatch.setattr(router_module, "billing_ineligibility_reason", lambda *_: None)
    monkeypatch.setattr(
        router_module, "rank",
        lambda *_: [Decision("groq", "openai/gpt-oss-20b", 1)],
    )
    path = tmp_path / "router.db"
    client = OfflineRetryClient()
    router = Router(client=client, store=RouterStore(path), max_retries=1)
    provider = router.add_provider("groq", "offline-key", discover=False)
    response = router.route(RouteRequest("offline retry", decompose=False))
    assert response.ok
    assert client.calls == 2
    assert [attempt.ok for attempt in response.attempts] == [False, True]
    state = provider.state.model("openai/gpt-oss-20b")
    assert (state.success_count, state.failure_count) == (1, 1)
    reloaded = Router(store=RouterStore(path)).add_provider("groq", "offline-key", discover=False)
    observed = reloaded.state.model("openai/gpt-oss-20b")
    assert (observed.success_count, observed.failure_count) == (1, 1)
    assert reloaded.state.success_count == 1
    assert reloaded.state.failure_count == 1
