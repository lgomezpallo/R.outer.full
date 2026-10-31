"""OpenRouter optional surcharge fields must be numeric when present.

All provider responses and account approvals are mocked in pytest; no real
inference, probe, network request, or account-side billing change is performed.
"""
from dataclasses import replace

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, ModelSpec, ProviderCatalog
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.selector import billing_ineligibility_reason, rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing

SURCHARGES = (
    "request", "image", "web_search", "internal_reasoning",
    "input_cache_read", "input_cache_write",
)
MODEL = "vendor/offline-price-fixture"


def _inventory(monkeypatch, pricing):
    spec = next(p for p in BUILTINS if p.id == "openrouter")
    provider = RegisteredProvider(spec, "offline-fake-key", RuntimeState())

    class FakeHTTP:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("trust_env") is False
            assert kwargs.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            assert url == "https://openrouter.ai/api/v1/models"
            return httpx.Response(
                200, request=httpx.Request("GET", url),
                json={"data": [{"id": MODEL, "pricing": pricing}]},
            )

        def post(self, *args, **kwargs):
            pytest.fail("discovery must not perform inference")

    monkeypatch.setattr(httpx, "Client", FakeHTTP)
    ids, statuses = ProviderClient().discover_inventory(provider)
    assert ids == [MODEL]
    return spec, statuses[MODEL]


@pytest.mark.parametrize("field", SURCHARGES)
def test_null_optional_surcharge_cannot_authorize_free_inference(monkeypatch, field):
    spec, status = _inventory(
        monkeypatch, {"prompt": "0", "completion": "0", field: None}
    )
    assert status == "unknown"
    # Isolate the per-model price check with test-only simulated account proof.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)
    catalog = ProviderCatalog((replace(spec, models=()),))
    updated = catalog.merge_discovered_models(spec.id, [MODEL], {MODEL: status})
    provider = RegisteredProvider(updated, "offline-fake-key", RuntimeState())
    model = updated.models[0]
    assert billing_ineligibility_reason(provider, model) == "billing_unverified"
    assert rank([provider], RouteRequest("hola", decompose=False)) == []

    monkeypatch.setattr(
        httpx, "Client",
        lambda *args, **kwargs: pytest.fail("unverified charge reached HTTP"),
    )
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified"):
        ProviderClient().complete(provider, MODEL, RouteRequest("hola"))


@pytest.mark.parametrize("charge,expected", [
    ("0", "free"),
    ("0.001", "paid"),
    ("not-a-number", "unknown"),
])
def test_numeric_optional_surcharge_classification(monkeypatch, charge, expected):
    _, status = _inventory(
        monkeypatch, {"prompt": "0", "completion": "0", "request": charge}
    )
    assert status == expected
