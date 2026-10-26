"""Routing-only provider aggregates: deterministic SQLite tests, no network."""

from router.state import RuntimeState
from router.storage import RouterStore


def _metric(store, rid, provider="groq", model="a", phase="direct",
            success=True, latency=100, error=None):
    store.record_metric(
        request_id=rid,
        application_name="offline-provider-report",
        provider_id=provider,
        model_id=model,
        phase=phase,
        attempt_number=1,
        latency_ms=latency,
        success=success,
        error_type=error,
    )


def test_provider_totals_equal_model_totals_without_probe_pollution(tmp_path):
    store = RouterStore(tmp_path / "metrics.db")
    _metric(store, "g-a-ok", latency=100)
    _metric(store, "g-a-timeout", success=False, latency=8000, error="timeout")
    _metric(store, "g-b-ok", model="b", latency=200)
    _metric(store, "n-fail", provider="nvidia", success=False,
            error="quota", latency=500)
    _metric(store, "g-probe", phase="probe:chat", latency=1)
    _metric(store, "g-audit", phase="audit:chat", success=False,
            latency=9000, error="probe_failure")

    models = store.model_performance_report()
    providers = {row["provider"]: row for row in store.provider_routing_report()}
    assert set(providers) == {"groq", "nvidia"}
    groq = providers["groq"]
    assert groq["attempts"] == 3
    assert groq["successful_requests"] == 2
    assert groq["failed_attempts"] == 1
    assert groq["models_observed"] == 2
    assert groq["success_rate"] == 0.6667
    assert groq["errors"] == {"timeout": 1}
    assert groq["observation_scope"] == "routing_attempts_only"
    assert providers["nvidia"]["errors"] == {"quota": 1}
    for provider_id, row in providers.items():
        subset = [m for m in models if m["provider"] == provider_id]
        assert row["attempts"] == sum(m["attempts"] for m in subset)
        assert row["successful_requests"] == sum(m["successful_requests"] for m in subset)
        assert row["failed_attempts"] == sum(
            m["attempts"] - m["successful_requests"] for m in subset
        )


def test_routing_report_does_not_rewrite_provider_health_or_cooldown(tmp_path):
    path = tmp_path / "metrics.db"
    store = RouterStore(path)
    _metric(store, "route-failure", success=False, error="timeout")
    _metric(store, "probe-ok", phase="probe:json", success=True)

    # Health/probe counters have a different scope from routing attempts.
    state = RuntimeState()
    state.mark_success("a", 25)
    state.mark_failure("a", "provider quota", 60, failure_kind="quota")
    store.save_provider_runtime("groq", state)
    before = store.load_provider_runtime("groq")
    assert before["cooldown_until"] > 0

    restarted = RouterStore(path)
    report = restarted.provider_routing_report()
    assert report[0]["attempts"] == 1
    assert report[0]["successful_requests"] == 0
    assert report[0]["errors"] == {"timeout": 1}
    assert restarted.load_provider_runtime("groq") == before


def test_provider_and_model_reports_use_same_bounded_routing_window(tmp_path):
    store = RouterStore(tmp_path / "metrics.db")
    _metric(store, "old", model="old", success=False, error="timeout")
    _metric(store, "new1", model="b", phase="subtask:code")
    for index in range(20):
        _metric(store, f"probe-{index}", phase="probe:chat")
    _metric(store, "new2", model="c", success=False, error="quota")

    # The LIMIT is applied to routing attempts, not all metric rows.
    models = store.model_performance_report(limit=2)
    providers = store.provider_routing_report(limit=2)
    assert {m["model"] for m in models} == {"b", "c"}
    assert len(providers) == 1
    assert providers[0]["attempts"] == 2
    assert providers[0]["successful_requests"] == 1
    assert providers[0]["errors"] == {"quota": 1}
