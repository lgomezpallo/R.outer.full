"""GO UTE candidate: offline-only HTTP contract and outbound hard-stop regressions.

No provider requests, discovery, probes, billing access, or external RSS.
"""
import pytest
from fastapi.testclient import TestClient

import router.api as api
from router.client import ProviderClient
from router.router import Router
from router.storage import RouterStore
from router import web_search

pytestmark = pytest.mark.strict_billing


def _offline_router(monkeypatch):
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    router = Router(client=ProviderClient())

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("offline startup attempted provider discovery")

    monkeypatch.setattr(router.client, "discover_inventory", unexpected_discovery)
    router.add_provider("Groq", "fixture-key-never-used", discover=True)
    return router


def test_offline_mode_denies_direct_inference_probe_and_inventory(monkeypatch):
    router = _offline_router(monkeypatch)
    provider = router.registry.all()[0]
    model = provider.spec.models[0].id
    with pytest.raises(RuntimeError, match="inference_blocked:offline_only"):
        router.client.complete(provider, model, __import__("router.types", fromlist=["RouteRequest"]).RouteRequest("hola"))
    with pytest.raises(RuntimeError, match="inference_blocked:offline_only"):
        router.client.probe_capability(provider, model, "chat")
    with pytest.raises(RuntimeError, match="inventory_blocked:offline_only"):
        ProviderClient().discover_inventory(provider)


def test_offline_mode_skips_rss_even_for_time_sensitive_tasks(monkeypatch):
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    monkeypatch.setattr(web_search.httpx, "get", lambda *a, **kw: pytest.fail("external RSS request"))
    monkeypatch.setattr(api, "search_context", lambda *a, **kw: pytest.fail("route attempted RSS"))
    assert web_search.search_current("noticias de hoy") == []
    assert "BÚSQUEDA ACTUAL" not in api._enriched_context("noticias de hoy", "")


def test_go_ute_http_contract_fails_closed_without_billing_authorization(monkeypatch, tmp_path):
    router = _offline_router(monkeypatch)
    monkeypatch.setattr(api, "router", router)
    monkeypatch.setattr(api, "store", RouterStore(tmp_path / "go-ute-offline.db"))
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "offline-service-token")
    monkeypatch.delenv("ROUTER_IACHAT_TOKEN", raising=False)
    monkeypatch.setattr(api, "search_context", lambda *a, **kw: pytest.fail("external search"))
    client = TestClient(api.app)
    auth = {"Authorization": "Bearer offline-service-token"}

    assert client.get("/health").status_code == 200
    assert client.post("/route", json={"task": "estado de obras hoy"}).status_code == 401
    response = client.post(
        "/route",
        headers=auth,
        json={
            "task": "estado de obras hoy",
            "application_name": "gestion-obras-ute",
            "required_capabilities": ["chat"],
            "decompose": False,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "no_eligible_provider"
    assert payload["attempts"] == []
    assert payload["text"] is None

    completion = client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "router-ia-auto", "messages": [{"role": "user", "content": "hola"}]},
    )
    assert completion.status_code == 502
    assert "no_eligible_provider" in completion.text
