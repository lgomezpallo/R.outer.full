from fastapi.testclient import TestClient
import router.api as module
from router.router import Router

def test_health():
    module.router = Router()
    client = TestClient(module.app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["providers"] == []

def test_add_provider_without_exposing_key(monkeypatch):
    module.router = Router()
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-token")
    client = TestClient(module.app)
    response = client.post(
        "/providers",
        headers={"Authorization":"Bearer admin-token"},
        json={"name":"Groc","api_key":"supersecret"},
    )
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


def test_health_reports_runtime_metrics():
    module.router = Router()
    module.router.add_provider("Groq", "secret")
    provider = module.router.registry.all()[0]
    provider.state.mark_success("openai/gpt-oss-20b", 120)
    client = TestClient(module.app)
    body = client.get("/health").json()
    assert body["providers"][0]["provider"] == "groq"
    assert body["providers"][0]["health_ok"] is True
    assert body["providers"][0]["latency_ms"] == 120.0
    assert body["providers"][0]["health_status"] == "available"
    model = body["providers"][0]["models"]["openai/gpt-oss-20b"]
    assert model["success_rate"] == 1.0
    assert model["latency_ms"] == 120.0


def test_catalog_lists_builtin_providers():
    module.router = Router()
    client = TestClient(module.app)
    body = client.get("/catalog").json()
    ids = {item["id"] for item in body["providers"]}
    assert "groq" in ids
    assert "openrouter" in ids

def test_catalog_can_add_provider_without_secret(monkeypatch):
    module.router = Router()
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-token")
    client = TestClient(module.app)
    response = client.post(
        "/catalog/providers",
        headers={"Authorization":"Bearer admin-token"},
        json={
        "id":"exampleai",
        "aliases":["example ai"],
        "base_url":"https://example.invalid/v1",
        "protocol":"openai-compatible",
        "models":[{
            "id":"example-model",
            "capabilities":["chat","json"],
            "model_class":"standard",
            "priority":55
        }]
    })
    assert response.status_code == 200
    catalog = client.get("/catalog").json()["providers"]
    item = next(provider for provider in catalog if provider["id"] == "exampleai")
    assert item["models"][0]["declared_capabilities"] == ["chat", "json"]
    assert "api_key" not in str(item).lower()
