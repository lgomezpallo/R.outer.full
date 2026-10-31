"""Cloudflare inventory is not a free-tier attestation or permission to spend."""
import httpx
import pytest

from router.billing import CLOUDFLARE_PAID_ONLY
from router.catalog import ProviderCatalog, ProviderSpec, BUILTINS
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest


BASE = "https://api.cloudflare.com/client/v4/accounts/test-account/ai"
PAID = "@cf/moonshotai/kimi-k2.6"


def _cloudflare():
    return ProviderSpec("cloudflare", ("cloudflare",), BASE, models=())


def test_cloudflare_paginates_entire_inventory_but_blocks_unverified_billing(monkeypatch, tmp_path):
    models = [f"@cf/vendor/chat-model-{i:03d}" for i in range(204)] + [PAID]
    seen = []

    class FakeHTTP:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            assert headers["Authorization"] == "Bearer fake-key"
            assert headers["Accept"] == "application/json"
            page = int(url.rsplit("page=", 1)[1])
            seen.append(page)
            batch = models[(page - 1) * 100:page * 100]
            return httpx.Response(
                200, request=httpx.Request("GET", url),
                json={"success": True, "result": [{"name": model} for model in batch]},
            )

    monkeypatch.setattr("router.client.httpx.Client", FakeHTTP)
    provider = RegisteredProvider(_cloudflare(), "fake-key", RuntimeState())
    discovered, billing = ProviderClient().discover_inventory(provider)
    assert len(discovered) == 205
    assert seen == [1, 2, 3]
    assert billing[PAID] == "paid"
    assert all(billing[model] == "unknown" for model in models[:-1])

    catalog = ProviderCatalog(providers=(_cloudflare(),), storage_path=tmp_path / "catalog.json")
    provider.spec = catalog.merge_discovered_models("cloudflare", discovered, billing)
    coverage = model_coverage([provider], RouteRequest("hola"))
    assert coverage["totals"]["cataloged_models"] == 205
    assert coverage["totals"]["eligible_models"] == 0
    assert coverage["providers"][0]["excluded_by_reason"] == {
        "billing_unverified": 204, "billing_paid": 1,
    }
    assert rank([provider], RouteRequest("hola")) == []
    reloaded = ProviderCatalog(providers=(_cloudflare(),), storage_path=tmp_path / "catalog.json")
    assert next(m for m in reloaded.get("cloudflare").models if m.id == PAID).billing_status == "paid"
    assert next(m for m in reloaded.get("cloudflare").models if m.id == models[0]).billing_status == "unknown"


def test_cloudflare_truncated_pagination_fails_instead_of_persisting_partial(monkeypatch):
    class EndlessHTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            return httpx.Response(
                200, request=httpx.Request("GET", url),
                json={"success": True, "result": [
                    {"name": f"@cf/vendor/chat-{i}"} for i in range(100)
                ]},
            )

    monkeypatch.setattr("router.client.httpx.Client", EndlessHTTP)
    with pytest.raises(RuntimeError, match="cloudflare_inventory_pagination_limit"):
        ProviderClient().discover_inventory(RegisteredProvider(_cloudflare(), "fake", RuntimeState()))


def test_cloudflare_billing_status_cannot_override_account_plan_check(tmp_path):
    catalog = ProviderCatalog(providers=(_cloudflare(),), storage_path=tmp_path / "catalog.json")
    catalog.merge_discovered_models("cloudflare", [PAID, "@cf/vendor/chat-free"], {
        PAID: "free", "@cf/vendor/chat-free": "free",
    })
    provider = RegisteredProvider(catalog.get("cloudflare"), "fake", RuntimeState())
    report = model_coverage([provider], RouteRequest("hola"))
    assert report["totals"]["eligible_models"] == 1
    assert report["providers"][0]["excluded_by_reason"] == {"billing_paid": 1}
    catalog.merge_discovered_models("cloudflare", ["@cf/vendor/chat-free"], {
        "@cf/vendor/chat-free": "unknown",
    })
    assert next(m for m in catalog.get("cloudflare").models if m.id == "@cf/vendor/chat-free").billing_status == "unknown"


class NoUnapprovedInference:
    def __init__(self):
        self.calls = []

    def complete(self, provider, model, req):
        assert provider.spec.id != "cloudflare"
        self.calls.append((provider.spec.id, model))
        return "OK", {}

    def probe_capability(self, provider, model, capability):
        raise AssertionError("paid_or_unverified_probe_was_sent")


def test_route_falls_back_to_groq_without_calling_cloudflare():
    groq = next(spec for spec in BUILTINS if spec.id == "groq")
    catalog = ProviderCatalog(providers=(_cloudflare(), groq))
    catalog.merge_discovered_models("cloudflare", ["@cf/vendor/chat-1"], {})
    client = NoUnapprovedInference()
    router = Router(client=client, catalog=catalog)
    router.add_provider("cloudflare", "fake", discover=False)
    router.add_provider("groq", "fake", discover=False)
    result = router.route(RouteRequest("hola"))
    assert result.ok and result.provider == "groq"
    assert client.calls and all(p == "groq" for p, _ in client.calls)


def test_active_probe_and_provider_test_never_call_unverified_cloudflare():
    catalog = ProviderCatalog(providers=(_cloudflare(),))
    catalog.merge_discovered_models("cloudflare", ["@cf/vendor/chat-1"], {})
    client = NoUnapprovedInference()
    router = Router(client=client, catalog=catalog)
    router.add_provider("cloudflare", "fake", discover=False)
    probe = router.verify_capability("cloudflare", "@cf/vendor/chat-1", "chat")
    assert probe["status"] == "inconclusive"
    assert probe["evidence"] == "probe_blocked:billing_unverified"
    assert probe["latency_ms"] == 0
    assert router.test_provider("cloudflare")["reason"] == "no_billing_verified_chat_model"
    assert not router.catalog.get("cloudflare").models[0].evidence
    assert not client.calls


def test_openrouter_paid_and_nvidia_unknown_active_probes_are_blocked():
    openrouter = next(spec for spec in BUILTINS if spec.id == "openrouter")
    nvidia = next(spec for spec in BUILTINS if spec.id == "nvidia")
    catalog = ProviderCatalog(providers=(openrouter, nvidia))
    catalog.merge_discovered_models("openrouter", ["vendor/chat-paid"], {"vendor/chat-paid": "paid"})
    catalog.merge_discovered_models("nvidia", ["vendor/chat-unknown"])
    client = NoUnapprovedInference()
    router = Router(client=client, catalog=catalog)
    router.add_provider("openrouter", "fake", discover=False)
    router.add_provider("nvidia", "fake", discover=False)
    for provider_id, model_id, reason in (
        ("openrouter", "vendor/chat-paid", "billing_paid"),
        ("nvidia", "vendor/chat-unknown", "billing_unverified"),
    ):
        probe = router.verify_capability(provider_id, model_id, "chat")
        assert probe["evidence"] == f"probe_blocked:{reason}"
        assert probe["status"] == "inconclusive"
        assert not next(m for m in router.catalog.get(provider_id).models if m.id == model_id).evidence


def test_budgeted_audit_does_not_reserve_budget_for_unverified_models(monkeypatch):
    from fastapi.testclient import TestClient
    import router.api as api

    catalog = ProviderCatalog(providers=(_cloudflare(),))
    catalog.merge_discovered_models("cloudflare", ["@cf/vendor/chat-1"])
    router = Router(client=NoUnapprovedInference(), catalog=catalog)
    router.add_provider("cloudflare", "fake", discover=False)

    class NoBudgetCalls:
        def audit_budget_used(self):
            return {}

        def audit_claimed_models(self, *args):
            # Synthetic store: no persisted claims, and no provider calls.
            return set()

        def reserve_audit_model(self, *args):
            raise AssertionError("blocked_model_consumed_audit_budget")

    monkeypatch.setattr(api, "router", router)
    monkeypatch.setattr(api, "store", NoBudgetCalls())
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "test-admin")
    response = TestClient(api.app).post(
        "/audit/step",
        headers={"Authorization": "Bearer test-admin"},
        json={"provider": "cloudflare", "max_calls": 3},
    )
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["pending_chat"] == 0
