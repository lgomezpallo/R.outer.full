"""P4: offline fixtures for explicitly free inventory and per-model fallback.

No real API credentials, network, provider calls, or production workflow.
"""
from __future__ import annotations

import httpx

from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import billing_ineligibility_reason
from router.state import RuntimeState
from router.types import RouteRequest


def _model(name: str, status: str, priority: int) -> ModelSpec:
    return ModelSpec(
        id=name, capabilities=frozenset({"chat"}), model_class="standard",
        priority=priority, strategic_cost=10, billing_status=status,
    )


def _provider(models: tuple[ModelSpec, ...]) -> ProviderSpec:
    return ProviderSpec(
        id="openrouter", aliases=("openrouter",),
        base_url="https://openrouter.ai/api/v1", models=models,
        discover_models=False,
    )


def test_openrouter_inventory_requires_zero_for_every_advertised_charge(monkeypatch):
    """A /models listing is not free evidence unless every price is zero."""
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "0")
    items = [
        {"id": "google/gemma-4-26b-a4b-it:free", "pricing": {"prompt": "0", "completion": "0", "request": "0"}},
        {"id": "openrouter/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "fixture/zero-but-funded", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "fixture/paid", "pricing": {"prompt": "0.01", "completion": "0"}},
        {"id": "fixture/surcharge", "pricing": {"prompt": "0", "completion": "0", "request": "0.001"}},
        {"id": "fixture/unknown"},
    ]
    original_client = httpx.Client
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.host == "openrouter.ai"
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json={"data": items})

    def local_client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("router.client.httpx.Client", local_client)
    spec = _provider(())
    registered = RegisteredProvider(spec, "fixture-not-a-real-key", RuntimeState())
    ids, prices = ProviderClient().discover_inventory(registered)
    assert ids == [item["id"] for item in items]
    assert prices == {
        "google/gemma-4-26b-a4b-it:free": "free",
        "openrouter/free": "free",
        "fixture/zero-but-funded": "unknown",
        "fixture/paid": "paid",
        "fixture/surcharge": "paid",
        "fixture/unknown": "unknown",
    }
    assert len(calls) == 1

    catalog = ProviderCatalog(providers=(spec,))
    updated = catalog.merge_discovered_models("openrouter", ids, prices)
    registered.spec = updated
    reasons = {
        model.id: billing_ineligibility_reason(registered, model)
        for model in updated.models
    }
    assert reasons["google/gemma-4-26b-a4b-it:free"] is None
    assert reasons["openrouter/free"] is None
    assert reasons["fixture/zero-but-funded"] == "billing_unverified"
    assert reasons["fixture/paid"] == "billing_paid"
    assert reasons["fixture/surcharge"] == "billing_paid"
    assert reasons["fixture/unknown"] == "billing_unverified"


def test_model_fallback_never_attempts_paid_or_unknown(monkeypatch):
    """One free model may fail; fallback stays within another explicit free model."""
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "0")

    class LocalClient:
        def __init__(self):
            self.calls = []

        def complete(self, provider, model, request):
            self.calls.append(model)
            if model == "nvidia/nemotron-3-ultra-550b-a55b:free":
                raise RuntimeError("simulated_model_specific_failure")
            if model != "google/gemma-4-26b-a4b-it:free":
                raise AssertionError("paid_or_unknown_dispatched")
            return "fixture-success", {"fixture": True}

    spec = _provider((
        _model("nvidia/nemotron-3-ultra-550b-a55b:free", "free", 100),
        _model("google/gemma-4-26b-a4b-it:free", "free", 90),
        _model("fixture/paid", "paid", 1000),
        _model("fixture/unknown", "unknown", 1000),
    ))
    client = LocalClient()
    router = Router(client=client, max_retries=0, catalog=ProviderCatalog(providers=(spec,)))
    router.add_provider("openrouter", "fixture-not-a-real-key", discover=False)
    response = router.route(RouteRequest(task="fixture text", decompose=False))
    assert response.ok
    assert response.model == "google/gemma-4-26b-a4b-it:free"
    assert client.calls == ["nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-26b-a4b-it:free"]
    assert [attempt.ok for attempt in response.attempts] == [False, True]
