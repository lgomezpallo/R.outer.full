"""Groq/NVIDIA inventory and historical allowlists cannot authorize unknown charges.

All tests use offline fixtures. No provider HTTP or account billing operations.
"""
from dataclasses import replace

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, ModelSpec, ProviderCatalog
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.selector import billing_ineligibility_reason, model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest


pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize("provider_id", ["groq", "nvidia"])
@pytest.mark.parametrize("status, expected", [
    ("unknown", "billing_unverified"),
    ("paid", "billing_paid"),
])
def test_unverified_groq_nvidia_model_never_reaches_http_even_with_mock_account(
    monkeypatch, provider_id, status, expected
):
    # Isolate the model-price gate by pretending, in this test ONLY, that the
    # separate account-side billing proof was already established.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)
    spec = next(p for p in BUILTINS if p.id == provider_id)
    model = replace(spec.models[0], billing_status=status)
    provider = RegisteredProvider(replace(spec, models=(model,)), "offline-fake-key", RuntimeState())
    req = RouteRequest("hola", decompose=False)
    assert billing_ineligibility_reason(provider, model) == expected
    assert rank([provider], req) == []
    coverage = model_coverage([provider], req)
    assert coverage["totals"]["eligible_models"] == 0
    assert coverage["providers"][0]["excluded_by_reason"] == {expected: 1}

    def forbid_http(*args, **kwargs):
        pytest.fail("unknown/paid price attempted to construct HTTP client")

    monkeypatch.setattr(httpx, "Client", forbid_http)
    for operation in (
        lambda: ProviderClient().complete(provider, model.id, req),
        lambda: ProviderClient().probe_capability(provider, model.id, "tools"),
        lambda: ProviderClient()._openai_compatible(provider, model.id, req),
    ):
        with pytest.raises(RuntimeError, match="inference_blocked:" + expected):
            operation()


@pytest.mark.parametrize("provider_id", ["groq", "nvidia"])
def test_discovery_replaces_stale_free_with_unknown_and_persists(tmp_path, provider_id):
    spec = next(p for p in BUILTINS if p.id == provider_id)
    catalog = ProviderCatalog((spec,), storage_path=tmp_path / "models.json")
    model_id = spec.models[0].id
    catalog.merge_discovered_models(provider_id, [model_id], {model_id: "free"})
    assert next(m for m in catalog.get(provider_id).models if m.id == model_id).billing_status == "free"

    # Provider /models normally returns IDs without reliable pricing. A
    # refresh must revoke old free status, not silently retain it.
    catalog.merge_discovered_models(provider_id, [model_id, "vendor/new-model"])
    reloaded = ProviderCatalog((spec,), storage_path=tmp_path / "models.json")
    states = {m.id: m.billing_status for m in reloaded.get(provider_id).models}
    assert states[model_id] == "unknown"
    assert states["vendor/new-model"] == "unknown"


@pytest.mark.parametrize("provider_id", ["groq", "nvidia"])
def test_provider_models_endpoint_does_not_infer_free_from_zero_looking_metadata(
    monkeypatch, provider_id
):
    spec = next(p for p in BUILTINS if p.id == provider_id)
    model_id = spec.models[0].id
    url = spec.base_url + "/models"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("trust_env") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, requested, **kwargs):
            assert requested == url
            return httpx.Response(
                200,
                request=httpx.Request("GET", requested),
                json={"data": [{"id": model_id, "pricing": {"prompt": "0", "completion": "0"}}]},
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    provider = RegisteredProvider(spec, "offline-fake-key", RuntimeState())
    ids, statuses = ProviderClient().discover_inventory(provider)
    assert ids == [model_id]
    assert statuses == {}
    catalog = ProviderCatalog((spec,))
    refreshed = catalog.merge_discovered_models(provider_id, ids, statuses)
    assert next(m for m in refreshed.models if m.id == model_id).billing_status == "unknown"
