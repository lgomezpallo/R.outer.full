"""Offline regression tests for phase-consistent model performance reporting.

No API keys, provider requests, probes, or network clients are used.
"""
from router.storage import RouterStore


def _attempt(store, request_id, *, provider="groq", model="model-a",
             phase="direct", success=True, latency=100, error_type=None):
    store.record_metric(
        request_id=request_id, application_name="offline-report",
        provider_id=provider, model_id=model, phase=phase,
        attempt_number=1, latency_ms=latency, success=success,
        error_type=error_type,
    )


def test_report_success_latency_excludes_timeouts_and_probes(tmp_path):
    store = RouterStore(tmp_path / "metrics.db")
    _attempt(store, "success-1", success=True, latency=120)
    _attempt(store, "timeout", success=False, latency=8800, error_type="timeout")
    _attempt(store, "success-2", phase="subtask:code", success=True, latency=240)
    _attempt(store, "probe", phase="probe:chat", success=True, latency=1)

    report = store.model_performance_report()
    assert len(report) == 1
    row = report[0]
    assert row["provider"] == "groq" and row["model"] == "model-a"
    assert row["attempts"] == 3
    assert row["successful_requests"] == 2
    assert row["success_rate"] == 0.6667
    assert row["avg_latency_ms"] == 180
    assert row["latency_samples"] == 2
    assert row["latency_scope"] == "successful_completions_only"
    assert row["avg_attempt_duration_ms"] == 3053
    assert row["errors"] == {"timeout": 1}
    stats = store.recent_model_stats()[("groq", "model-a")]
    assert stats["samples"] == row["attempts"]
    assert stats["success_count"] == row["successful_requests"]


def test_report_without_success_has_unknown_completion_latency(tmp_path):
    store = RouterStore(tmp_path / "metrics.db")
    _attempt(store, "failure-1", success=False, latency=1200, error_type="timeout")
    _attempt(store, "failure-2", success=False, latency=800, error_type="http_503")
    row = store.model_performance_report()[0]
    assert row["attempts"] == 2
    assert row["successful_requests"] == 0
    assert row["success_rate"] == 0.0
    assert row["avg_latency_ms"] is None
    assert row["latency_samples"] == 0
    assert row["avg_attempt_duration_ms"] == 1000
    assert row["errors"] == {"timeout": 1, "http_503": 1}


def test_report_keeps_provider_model_buckets_and_excludes_audits(tmp_path):
    store = RouterStore(tmp_path / "metrics.db")
    _attempt(store, "groq-ok", success=True, latency=150)
    _attempt(store, "nvidia-ok", provider="nvidia", success=True, latency=350)
    _attempt(store, "groq-audit", phase="audit:chat", success=False, latency=9900)
    _attempt(store, "nvidia-probe", provider="nvidia", phase="probe:json",
             success=True, latency=2)
    rows = {(row["provider"], row["model"]): row for row in store.model_performance_report()}
    assert set(rows) == {("groq", "model-a"), ("nvidia", "model-a")}
    assert rows[("groq", "model-a")]["avg_latency_ms"] == 150
    assert rows[("nvidia", "model-a")]["avg_latency_ms"] == 350
    assert all(row["attempts"] == 1 and row["latency_samples"] == 1 for row in rows.values())
