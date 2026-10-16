from fastapi.testclient import TestClient
import router.api as module
from router.storage import RouterStore


def test_diagnostics_requires_admin_token_and_excludes_content(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-only-token")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "iachat-token")
    monkeypatch.setattr(module, "store", RouterStore(tmp_path / "router.db"))
    module.store.record_metric(
        request_id="request-1", application_name="iachat",
        provider_id="groq", model_id="test-model", phase="execute",
        attempt_number=1, latency_ms=125, success=False,
        error_type="rate_limit", error_code=429,
    )
    client = TestClient(module.app)
    path = "/diagnostics/requests"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer iachat-token"}).status_code == 401
    response = client.get(path, headers={"Authorization": "Bearer admin-only-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["timing_scope"] == "provider_attempts_only"
    assert body["requests"][0]["attempts"][0]["error_code"] == 429
    assert "admin-only-token" not in response.text
    assert "iachat-token" not in response.text
    assert "task" not in body["requests"][0]


def test_diagnostics_limit_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-only-token")
    monkeypatch.setattr(module, "store", RouterStore(tmp_path / "router.db"))
    client = TestClient(module.app)
    response = client.get(
        "/diagnostics/requests?limit=100000",
        headers={"Authorization": "Bearer admin-only-token"},
    )
    assert response.status_code == 200
    assert response.json()["requests"] == []
