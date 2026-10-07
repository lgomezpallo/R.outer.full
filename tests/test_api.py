from fastapi.testclient import TestClient
import router.api as module
from router.router import Router

def test_health():
    module.router = Router()
    client = TestClient(module.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_add_provider_without_exposing_key():
    module.router = Router()
    client = TestClient(module.app)
    response = client.post("/providers", json={"name":"Groc","api_key":"supersecret"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "groq"
    assert "supersecret" not in response.text


def test_service_token_protects_route(monkeypatch):
    module.router = Router()
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "secret-token")
    client = TestClient(module.app)
    denied = client.post("/route", json={"task":"hola"})
    assert denied.status_code == 401
    allowed = client.post(
        "/route",
        headers={"Authorization":"Bearer secret-token"},
        json={"task":"hola"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["error"] == "no_eligible_provider"
