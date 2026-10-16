from router.storage import RouterStore


def test_recent_request_traces_group_filter_and_privacy(tmp_path):
    store = RouterStore(path=tmp_path / "router.db")
    for request_id, app, provider, attempt, success, error in [
        ("r1", "iachat", "groq", 1, False, "rate_limit"),
        ("r1", "iachat", "nvidia", 2, True, None),
        ("r2", "other", "cloudflare", 1, True, None),
    ]:
        store.record_metric(
            request_id=request_id, application_name=app, provider_id=provider,
            model_id="test-model", phase="execute", attempt_number=attempt,
            latency_ms=100 * attempt, success=success, error_type=error,
        )
    traces = store.recent_request_traces(application_name="iachat")
    assert len(traces) == 1
    trace = traces[0]
    assert trace["request_id"] == "r1"
    assert trace["fallback"] is True
    assert trace["total_provider_latency_ms"] == 300
    assert [a["provider"] for a in trace["attempts"]] == ["groq", "nvidia"]
    assert trace["attempts"][0]["error_type"] == "rate_limit"
    assert store.recent_request_traces(limit=1)[0]["request_id"] == "r2"
    assert store.recent_request_traces(application_name="missing") == []
    assert "prompt" not in str(traces)
    assert "api_key" not in str(traces)
