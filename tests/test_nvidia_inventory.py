"""NVIDIA discovery inventory must not silently expand the billable routing set."""
import httpx

from router.billing import NVIDIA_ROUTING_ALLOWLIST
from router.catalog import ProviderCatalog, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest


NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"


def _provider():
    return ProviderSpec(
        id="nvidia", aliases=("nvidia",), base_url=NVIDIA_BASE, models=(),
    )


def test_nvidia_discovers_130_unknown_models_without_approving_billing(monkeypatch, tmp_path):
    unknown = [f"vendor/chat-model-{i}" for i in range(130)]
    previously_routable = [
        "moonshotai/kimi-k3", "google/gemma-4-31b-it",
    ]
    items = [{"id": model} for model in unknown + previously_routable]
    items.extend([{"id": unknown[0]}, {"id": ""}, {"id": "x" * 201}])

    class FakeHTTPClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            assert url == NVIDIA_BASE + "/models"
            assert headers["Authorization"] == "Bearer test-only"
            return httpx.Response(200, json={"data": items}, request=httpx.Request("GET", url))

    monkeypatch.setattr("router.client.httpx.Client", FakeHTTPClient)
    spec = _provider()
    registered = RegisteredProvider(spec, "test-only", RuntimeState())
    discovered = ProviderClient().discover_models(registered)
    assert len(discovered) == 132
    assert len(set(discovered)) == 132

    path = tmp_path / "catalog.json"
    catalog = ProviderCatalog(providers=(spec,), storage_path=path)
    registered.spec = catalog.merge_discovered_models("nvidia", discovered)
    report = model_coverage([registered], RouteRequest("hola"))
    assert report["totals"]["cataloged_models"] == 132
    assert report["totals"]["eligible_models"] == 2
    assert report["totals"]["excluded_models"] == 130
    assert report["providers"][0]["excluded_by_reason"] == {"billing_unverified": 130}
    assert len(rank([registered], RouteRequest("hola"))) == 2
    assert all(dec.model in NVIDIA_ROUTING_ALLOWLIST for dec in rank([registered], RouteRequest("hola")))

    # A restarted catalog still distinguishes inventory from approved routing.
    loaded = ProviderCatalog(providers=(spec,), storage_path=path)
    registered.spec = loaded.get("nvidia")
    assert len(registered.spec.models) == 132
    assert model_coverage([registered], RouteRequest("hola"))["totals"]["eligible_models"] == 2

    # Capability verification does not certify a model's price or authorize billing.
    loaded.record_capability("nvidia", unknown[0], "chat", "verified", "synthetic_test")
    registered.spec = loaded.get("nvidia")
    assert model_coverage([registered], RouteRequest("hola"))["totals"]["eligible_models"] == 2


def test_route_never_attempts_new_nvidia_model_without_billing_approval():
    from router.catalog import ModelSpec
    spec = ProviderSpec(
        "nvidia", ("nvidia",), NVIDIA_BASE,
        models=(
            ModelSpec("vendor/unknown-chat", frozenset({"chat"}), "strong", 1000, 1),
            ModelSpec("moonshotai/kimi-k3", frozenset({"chat"}), "standard", 50, 35),
        ),
    )

    class NoUnapprovedCalls:
        def __init__(self):
            self.calls = []

        def complete(self, provider, model, req):
            assert model in NVIDIA_ROUTING_ALLOWLIST
            self.calls.append(model)
            return "ROUTER_OK", {}

    client = NoUnapprovedCalls()
    router = Router(client=client, catalog=ProviderCatalog(providers=(spec,)))
    router.add_provider("nvidia", "fake-key", discover=False)
    result = router.route(RouteRequest("hola"))
    assert result.ok
    assert result.model == "moonshotai/kimi-k3"
    assert client.calls == ["moonshotai/kimi-k3"]
    assert len(result.decisions) == 1
