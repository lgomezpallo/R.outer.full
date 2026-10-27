"""Stage 7: request-level fallback metrics are offline, read-only observations.

No provider inference, probes, billing calls or external network requests.
"""
from fastapi.testclient import TestClient

import router.api as api
from router.storage import RouterStore


def _metric(store, request, attempt, provider, model, success, phase="execute"):
    store.record_metric(
        request_id=request, application_name="offline-fixture",
        provider_id=provider, model_id=model, phase=phase,
        attempt_number=attempt, latency_ms=10, success=success,
        error_type=None if success else "synthetic_timeout",
    )


def _seed(store):
    _metric(store, "single", 1, "groq", "a", True)
    _metric(store, "retry", 1, "groq", "a", False)
    _metric(store, "retry", 2, "groq", "a", True)
    _metric(store, "model-fallback", 1, "groq", "a", False)
    _metric(store, "model-fallback", 2, "groq", "b", True)
    _metric(store, "provider-fallback", 1, "groq", "a", False)
    _metric(store, "provider-fallback", 2, "nvidia", "c", True)
    _metric(store, "failed", 1, "groq", "a", False)
    _metric(store, "probe-only", 1, "groq", "a", True, phase="probe:chat")
    _metric(store, "audit-only", 1, "groq", "a", True, phase="audit:chat")


def test_fallback_report_distinguishes_retry_model_and_provider(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _seed(store)
    report = store.routing_fallback_report()
    assert report["sampled_requests"] == 5
    assert report["attempts"] == 8
    assert report["successful_attempts"] == 4
    assert report["successful_requests"] == 4
    assert report["failed_requests"] == 1
    assert report["requests_with_same_model_retry"] == 1
    assert report["requests_with_model_fallback"] == 2
    assert report["requests_with_provider_fallback"] == 1
    assert report["requests_with_same_provider_model_switch"] == 1
    assert report["requests_with_provider_only_fallback"] == 0
    assert report["same_model_retry_transitions"] == 1
    assert report["requests_recovered_after_failure"] == 3
    assert report["attempt_success_rate"] == 0.5
    assert report["request_success_rate"] == 0.8
    assert report["quality_measured"] is False
    assert report["billing_verified"] is False


def test_recent_request_limit_does_not_truncate_fallback_attempts(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _seed(store)
    # The last two *routing requests* have three attempts total.
    report = store.routing_fallback_report(limit=2)
    assert report["sampled_requests"] == 2
    assert report["attempts"] == 3
    assert report["requests_with_provider_fallback"] == 1
    assert report["successful_requests"] == 1
    assert store.routing_fallback_report(limit=0)["sampled_requests"] == 1


def test_empty_metrics_are_not_reported_as_zero_percent_quality(tmp_path):
    report = RouterStore(tmp_path / "router.db").routing_fallback_report()
    assert report["sampled_requests"] == 0
    assert report["attempt_success_rate"] is None
    assert report["request_success_rate"] is None
    assert report["snapshot_caveat"] == "in_flight_requests_may_be_incomplete"


def test_fallback_endpoint_is_admin_only_and_contains_no_content(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-only-token")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "iachat-only-token")
    store = RouterStore(tmp_path / "router.db")
    _seed(store)
    monkeypatch.setattr(api, "store", store)
    client = TestClient(api.app)
    path = "/diagnostics/routing-fallback?limit=2"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer iachat-only-token"}).status_code == 401
    response = client.get(path, headers={"Authorization": "Bearer admin-only-token"})
    assert response.status_code == 200
    assert response.json()["sampled_requests"] == 2
    assert response.json()["requests_with_provider_fallback"] == 1
    assert "admin-only-token" not in response.text
    assert "iachat-only-token" not in response.text
    assert "offline-fixture" not in response.text


def test_nonadjacent_return_to_model_is_not_a_same_model_retry(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _metric(store, "return-to-a", 1, "groq", "a", False)
    _metric(store, "return-to-a", 2, "groq", "b", False)
    _metric(store, "return-to-a", 3, "groq", "a", True)
    report = store.routing_fallback_report()
    assert report["requests_with_same_model_retry"] == 0
    assert report["same_model_retry_transitions"] == 0
    assert report["requests_with_model_fallback"] == 1
    assert report["requests_with_same_provider_model_switch"] == 1
    assert report["requests_recovered_after_failure"] == 1


def test_provider_only_fallback_is_not_a_same_provider_model_switch(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _metric(store, "provider-only", 1, "groq", "shared-id", False)
    _metric(store, "provider-only", 2, "nvidia", "shared-id", True)
    report = store.routing_fallback_report()
    assert report["requests_with_provider_fallback"] == 1
    assert report["requests_with_provider_only_fallback"] == 1
    assert report["requests_with_same_provider_model_switch"] == 0
    # The legacy key means any changed provider/model pair.
    assert report["requests_with_model_fallback"] == 1
    assert report["requests_with_same_model_retry"] == 0


def test_repeated_failed_model_attempts_count_transitions_once_per_request(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    _metric(store, "triple", 1, "groq", "a", False)
    _metric(store, "triple", 2, "groq", "a", False)
    _metric(store, "triple", 3, "groq", "a", True)
    report = store.routing_fallback_report()
    assert report["requests_with_same_model_retry"] == 1
    assert report["same_model_retry_transitions"] == 2
    assert report["requests_with_model_fallback"] == 0
    assert report["requests_recovered_after_failure"] == 1


def test_successful_attempt_does_not_trigger_fallback_transition(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    # Synthetic corrupted/overlapping records must not invent a fallback
    # after an already successful attempt.
    _metric(store, "success-first", 1, "groq", "a", True)
    _metric(store, "success-first", 2, "nvidia", "b", True)
    report = store.routing_fallback_report()
    assert report["requests_with_model_fallback"] == 0
    assert report["requests_with_provider_fallback"] == 0
    assert report["requests_with_same_model_retry"] == 0
