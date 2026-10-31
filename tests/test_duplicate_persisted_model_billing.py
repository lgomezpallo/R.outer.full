"""Offline regression: persisted duplicate model IDs never authorize inference.

Discovery already coalesces duplicate OpenRouter records, but a manually edited
or older persisted catalog may still contain duplicate IDs. A free row must
never mask a paid or unknown row, even if a custom selector is injected.
Every account approval below is simulated only inside pytest.
"""
from dataclasses import replace
from importlib import import_module

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, ModelSpec, ProviderCatalog, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import billing_ineligibility_reason, rank
from router.state import RuntimeState
from router.types import Decision, RouteRequest

pytestmark = pytest.mark.strict_billing

MODEL_IDS = {
    "groq": "openai/gpt-oss-20b",
    "nvidia": "google/gemma-4-31b-it",
    "cloudflare": "@cf/meta/llama-3.1-8b-instruct",
    "openrouter": "vendor/verified-zero",
}


def _duplicate_spec(provider_id, statuses):
    if provider_id == "cloudflare":
        spec = ProviderSpec(
            id="cloudflare",
            aliases=("cloudflare",),
            base_url="https://api.cloudflare.com/client/v4/accounts/offline/ai",
            models=(),
        )
    else:
        spec = next(item for item in BUILTINS if item.id == provider_id)
    model_id = MODEL_IDS[provider_id]
    models = tuple(
        ModelSpec(model_id, frozenset({"chat"}), "standard", billing_status=status)
        for status in statuses
    )
    return replace(spec, models=models)


@pytest.mark.parametrize("provider_id", sorted(MODEL_IDS))
@pytest.mark.parametrize("statuses", [("free", "paid"), ("paid", "free"), ("free", "free")])
def test_duplicate_catalog_models_block_ranking_transport_and_probes(
    monkeypatch, provider_id, statuses
):
    # This isolates model identity from the independent production account
    # gate, which continues to deny all real provider accounts.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)
    provider = RegisteredProvider(
        _duplicate_spec(provider_id, statuses), "offline-fake-key", RuntimeState()
    )
    model_id = MODEL_IDS[provider_id]
    assert all(
        billing_ineligibility_reason(provider, model) == "billing_ambiguous_model"
        for model in provider.spec.models
    )
    assert rank([provider], RouteRequest("hola", decompose=False)) == []

    def forbid_http(*args, **kwargs):
        pytest.fail("duplicate model reached HTTP construction")

    monkeypatch.setattr(httpx, "Client", forbid_http)
    client = ProviderClient()
    for operation in (
        lambda: client.complete(provider, model_id, RouteRequest("hola")),
        lambda: client.probe_capability(provider, model_id, "vision"),
        lambda: client._openai_compatible(provider, model_id, RouteRequest("hola")),
    ):
        with pytest.raises(RuntimeError, match="inference_blocked:billing_ambiguous_model"):
            operation()


@pytest.mark.parametrize("provider_id", sorted(MODEL_IDS))
def test_persisted_duplicate_catalog_cannot_be_routed_by_injected_selector(
    monkeypatch, tmp_path, provider_id
):
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)
    spec = _duplicate_spec(provider_id, ("free", "paid"))
    storage = tmp_path / "catalog.json"
    ProviderCatalog((spec,), storage_path=storage).save()
    restored = ProviderCatalog((spec,), storage_path=storage)
    assert len(restored.get(provider_id).models) == 2

    class UnsafeInjectedClient:
        def complete(self, *args, **kwargs):
            pytest.fail("forged ranking reached custom inference client")

    router = Router(client=UnsafeInjectedClient(), catalog=restored)
    router.add_provider(provider_id, "offline-fake-key", discover=False)
    routing_module = import_module("router.router")
    monkeypatch.setattr(
        routing_module, "rank",
        lambda *args: [Decision(provider_id, MODEL_IDS[provider_id], 999, ["forged"])],
    )
    result = router.route(RouteRequest("hola", decompose=False))
    assert not result.ok
    assert result.error == "no_eligible_provider"
    assert result.attempts == []
