"""OpenRouter inventory is not an authorization to spend money."""
import httpx

from router.catalog import ProviderCatalog, BUILTINS
from router.client import ProviderClient
from router.router import Router
from router.selector import model_coverage, rank
from router.types import RouteRequest


def _provider_spec():
    return next(spec for spec in BUILTINS if spec.id == "openrouter")


def _install_models(monkeypatch, entries):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            assert url == "https://openrouter.ai/api/v1/models"
            return httpx.Response(200, request=httpx.Request("GET", url), json={"data": entries})

    monkeypatch.setattr("router.client.httpx.Client", FakeClient)


def test_openrouter_discovers_all_but_ranks_only_verified_free(monkeypatch, tmp_path):
    entries = [
        {"id": f"vendor/paid-{i}", "pricing": {"prompt": "0.001", "completion": "0"}}
        for i in range(130)
    ] + [
        {"id": "vendor/free-zero:free", "pricing": {"prompt": "0", "completion": "0.000"}},
        {"id": "vendor/missing-price"},
        {"id": "vendor/misleading:free", "pricing": {"prompt": "0.2", "completion": "0"}},
        {"id": "vendor/nan-price", "pricing": {"prompt": "NaN", "completion": "0"}},
        {"id": "vendor/request-surcharge", "pricing": {"prompt": "0", "completion": "0", "request": "0.01"}},
    ]
    _install_models(monkeypatch, entries)
    catalog = ProviderCatalog(providers=(_provider_spec(),), storage_path=tmp_path / "catalog.json")
    router = Router(client=ProviderClient(), catalog=catalog)
    provider = router.add_provider("openrouter", "fake-key")
    models = {m.id: m for m in provider.spec.models}
    assert len(models) == 136  # 135 discovered + built-in openrouter/free
    assert models["vendor/free-zero:free"].billing_status == "free"
    assert models["vendor/misleading:free"].billing_status == "paid"
    assert models["vendor/missing-price"].billing_status == "unknown"
    selected = {d.model for d in rank([provider], RouteRequest("hola"))}
    # Legacy openrouter/free without explicit zero-price evidence is blocked.
    assert selected == {"vendor/free-zero:free"}
    coverage = model_coverage([provider], RouteRequest("hola"))
    assert coverage["totals"]["cataloged_models"] == 136
    assert coverage["totals"]["eligible_models"] == 1
    assert coverage["providers"][0]["excluded_by_reason"]["billing_paid"] == 132
    assert coverage["providers"][0]["excluded_by_reason"]["billing_unverified"] == 3
    # Reloading the catalog cannot turn paid or unknown models into eligible.
    loaded = ProviderCatalog(providers=(_provider_spec(),), storage_path=tmp_path / "catalog.json")
    assert {m.id: m.billing_status for m in loaded.get("openrouter").models} == {
        m.id: m.billing_status for m in provider.spec.models
    }


def test_price_change_and_disappearance_revoke_previous_free_approval(tmp_path):
    catalog = ProviderCatalog(providers=(_provider_spec(),), storage_path=tmp_path / "catalog.json")
    catalog.merge_discovered_models("openrouter", ["vendor/zero"], {"vendor/zero": "free"})
    assert catalog.get("openrouter").models[-1].billing_status == "free"
    catalog.merge_discovered_models("openrouter", ["vendor/zero"], {"vendor/zero": "paid"})
    assert catalog.get("openrouter").models[-1].billing_status == "paid"
    catalog.merge_discovered_models("openrouter", ["vendor/zero"], {"vendor/zero": "free"})
    catalog.merge_discovered_models("openrouter", ["vendor/other"], {"vendor/other": "paid"})
    assert next(m for m in catalog.get("openrouter").models if m.id == "vendor/zero").billing_status == "unknown"


def test_discover_models_legacy_returns_complete_inventory(monkeypatch):
    entries = [{"id": "vendor/paid", "pricing": {"prompt": "0.1", "completion": "0.2"}},
               {"id": "vendor/free", "pricing": {"prompt": "0", "completion": "0"}}]
    _install_models(monkeypatch, entries)
    from router.registry import RegisteredProvider
    from router.state import RuntimeState
    provider = RegisteredProvider(_provider_spec(), "fake", RuntimeState())
    assert ProviderClient().discover_models(provider) == ["vendor/paid", "vendor/free"]


def test_new_pricing_fields_fail_closed_without_inference(monkeypatch):
    """A newly introduced billing dimension must never inherit a free label."""
    import pytest
    from router.registry import RegisteredProvider
    from router.state import RuntimeState

    entries = [
        {"id": "vendor/verified-zero:free", "pricing": {
            "prompt": "0", "completion": "0", "request": "0",
            "image": "0", "web_search": "0", "internal_reasoning": "0",
            "input_cache_read": "0", "input_cache_write": "0",
        }},
        {"id": "vendor/unknown-audio-charge", "pricing": {
            "prompt": "0", "completion": "0", "audio": "0.5",
        }},
        {"id": "vendor/unknown-zero-charge", "pricing": {
            "prompt": "0", "completion": "0", "future_fee": "0",
        }},
        {"id": "vendor/invalid-charge", "pricing": {
            "prompt": "0", "completion": "0", "request": "NaN",
        }},
    ]
    _install_models(monkeypatch, entries)
    provider = RegisteredProvider(_provider_spec(), "fake-key", RuntimeState())
    client = ProviderClient()
    model_ids, prices = client.discover_inventory(provider)
    assert len(model_ids) == 4
    assert prices == {
        "vendor/verified-zero:free": "free",
        "vendor/unknown-audio-charge": "unknown",
        "vendor/unknown-zero-charge": "unknown",
        "vendor/invalid-charge": "unknown",
    }

    catalog = ProviderCatalog(providers=(_provider_spec(),))
    provider.spec = catalog.merge_discovered_models("openrouter", model_ids, prices)
    decisions = rank([provider], RouteRequest("hola"))
    assert [d.model for d in decisions] == ["vendor/verified-zero:free"]

    # The client must reject direct calls before constructing any HTTP POST.
    for blocked_model in (
        "vendor/unknown-audio-charge",
        "vendor/unknown-zero-charge",
        "vendor/invalid-charge",
    ):
        with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified"):
            client.complete(provider, blocked_model, RouteRequest("hola"))
