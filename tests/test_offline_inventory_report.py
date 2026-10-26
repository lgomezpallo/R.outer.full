"""Offline inventory provenance, pricing and billing eligibility are separate."""
import pytest
from fastapi.testclient import TestClient

from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.inventory import inventory_report
from router.registry import RegisteredProvider
from router.router import Router
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing

ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "cloudflare": "https://api.cloudflare.com/client/v4/accounts/test_account/ai",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _provider(name, models):
    spec = ProviderSpec(
        name, (name,), ENDPOINTS[name],
        tuple(ModelSpec(model_id, frozenset({"chat"}), "standard",
                        billing_status=price,
                        discovered_in_last_snapshot=discovered)
              for model_id, price, discovered in models),
    )
    return RegisteredProvider(spec, "offline-placeholder", RuntimeState())


def test_four_provider_inventory_keeps_price_discovery_and_eligibility_separate():
    providers = [
        _provider("groq", [("g-free", "free", True), ("g-unknown", "unknown", False)]),
        _provider("nvidia", [("n-paid", "paid", True)]),
        _provider("cloudflare", [("@cf/example/chat", "unknown", True)]),
        _provider("openrouter", [("o-free", "free", True)]),
    ]
    report = inventory_report(providers)
    assert report["live_discovery_performed"] is False
    assert report["discovery_freshness"] == "not_verified"
    assert report["account_billing_verified"] is False
    assert report["totals"] == {
        "cataloged": 5,
        "discovered_in_last_snapshot": 4,
        "reported_free_price": 2,
        "reported_paid_price": 1,
        "unknown_price": 2,
        "eligible": 0,
        "blocked": 5,
    }
    groq = report["providers"][0]
    assert groq["models"][0]["reported_price"] == "free"
    assert groq["models"][0]["eligible_for_inference"] is False
    assert groq["models"][0]["blocked_reason"] == "billing_unverified_account"
    assert groq["models"][1]["discovered_in_last_snapshot"] is False
    assert report["providers"][1]["models"][0]["blocked_reason"] == "billing_paid"
    assert report["providers"][2]["models"][0]["blocked_reason"] == "billing_unverified"


def test_unconfigured_providers_do_not_look_discovered_or_eligible():
    report = inventory_report([])
    assert len(report["providers"]) == 4
    assert all(not row["configured"] and row["models"] == [] for row in report["providers"])
    assert report["totals"]["eligible"] == report["totals"]["cataloged"] == 0


def test_duplicate_catalog_models_are_reported_blocked():
    provider = _provider("openrouter", [
        ("same-model", "free", True),
        ("same-model", "paid", True),
    ])
    report = inventory_report([provider])
    models = report["providers"][3]["models"]
    assert len(models) == 2
    assert all(item["blocked_reason"] == "billing_ambiguous_model" for item in models)
    assert report["totals"]["eligible"] == 0


def test_discovery_provenance_survives_persistence_and_revokes_missing_models(tmp_path):
    path = tmp_path / "inventory.json"
    spec = ProviderSpec("groq", ("groq",), ENDPOINTS["groq"], (
        ModelSpec("older", frozenset({"chat"}), "standard", billing_status="free"),
    ))
    catalog = ProviderCatalog(providers=(spec,), storage_path=path)
    first = catalog.merge_discovered_models(
        "groq", ["older", "new"], {"older": "free", "new": "unknown"}
    )
    assert all(m.discovered_in_last_snapshot for m in first.models)
    second = catalog.merge_discovered_models("groq", ["new"], {"new": "unknown"})
    old = next(m for m in second.models if m.id == "older")
    assert old.discovered_in_last_snapshot is False
    assert old.billing_status == "unknown"
    restored = ProviderCatalog(providers=(), storage_path=path)
    restored_models = {m.id: m for m in restored.get("groq").models}
    assert restored_models["older"].discovered_in_last_snapshot is False
    assert restored_models["new"].discovered_in_last_snapshot is True


def test_inventory_endpoint_is_admin_only_and_never_touches_network(monkeypatch):
    import router.api as api

    class NoNetworkClient:
        def complete(self, *args, **kwargs):
            raise AssertionError("inventory_cannot_infer")
        def discover_inventory(self, *args, **kwargs):
            raise AssertionError("inventory_cannot_discover")
        def probe_capability(self, *args, **kwargs):
            raise AssertionError("inventory_cannot_probe")

    provider = _provider("groq", [("free-model", "free", True)])
    catalog = ProviderCatalog(providers=(provider.spec,))
    instance = Router(client=NoNetworkClient(), catalog=catalog)
    instance.add_provider("groq", "offline-placeholder", discover=False)
    monkeypatch.setattr(api, "router", instance)
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "offline-admin")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "offline-app")
    client = TestClient(api.app)
    assert client.get("/diagnostics/inventory").status_code == 401
    assert client.get("/diagnostics/inventory", headers={
        "Authorization": "Bearer offline-app"
    }).status_code == 401
    response = client.get("/diagnostics/inventory", headers={
        "Authorization": "Bearer offline-admin"
    })
    assert response.status_code == 200
    assert response.json()["totals"]["eligible"] == 0
    assert response.json()["totals"]["reported_free_price"] == 1
    assert "offline-placeholder" not in response.text
    assert "offline-admin" not in response.text
