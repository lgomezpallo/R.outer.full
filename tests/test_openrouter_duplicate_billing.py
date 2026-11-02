"""Offline regression: duplicate catalog rows cannot launder billable models.

All provider HTTP responses are mocked. No credentials, inference APIs, or
billing settings are accessed. Account authorization is simulated in pytest
only to isolate the per-model pricing gate from the account-level deny-all.
"""
import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, ProviderCatalog
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.selector import billing_ineligibility_reason, rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_openrouter_ids_take_most_restrictive_price(monkeypatch, reverse):
    zero = {"prompt": "0", "completion": "0"}
    charged = {"prompt": "0.001", "completion": "0"}
    entries = [
        {"id": "vendor/conflicting", "pricing": zero},
        {"id": "vendor/conflicting", "pricing": charged},
        {"id": "vendor/uncertain", "pricing": zero},
        {"id": "vendor/uncertain", "pricing": {"prompt": "0"}},
        {"id": "vendor/zero:free", "pricing": zero},
        {"id": "vendor/zero:free", "pricing": zero},
    ]
    if reverse:
        entries.reverse()

    class InventoryOnlyHTTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            assert url == "https://openrouter.ai/api/v1/models"
            return httpx.Response(
                200, request=httpx.Request("GET", url), json={"data": entries}
            )

    monkeypatch.setattr(httpx, "Client", InventoryOnlyHTTP)
    spec = next(item for item in BUILTINS if item.id == "openrouter")
    provider = RegisteredProvider(spec, "offline-fixture", RuntimeState())
    client = ProviderClient()
    ids, prices = client.discover_inventory(provider)
    expected_ids = (
        ["vendor/zero:free", "vendor/uncertain", "vendor/conflicting"]
        if reverse else ["vendor/conflicting", "vendor/uncertain", "vendor/zero:free"]
    )
    assert ids == expected_ids
    assert prices == {
        "vendor/conflicting": "paid",
        "vendor/uncertain": "unknown",
        "vendor/zero:free": "free",
    }

    provider.spec = ProviderCatalog(providers=(spec,)).merge_discovered_models(
        "openrouter", ids, prices
    )
    # Simulated only: verifies that model billing still blocks requests even
    # if an account were separately established to have no billable overages.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)
    models = {model.id: model for model in provider.spec.models}
    assert billing_ineligibility_reason(provider, models["vendor/conflicting"]) == "billing_paid"
    assert billing_ineligibility_reason(provider, models["vendor/uncertain"]) == "billing_unverified"
    assert [d.model for d in rank([provider], RouteRequest("hello"))] == ["vendor/zero:free"]

    def no_inference_client(*args, **kwargs):
        pytest.fail("billable or uncertain model constructed an inference HTTP client")

    monkeypatch.setattr(httpx, "Client", no_inference_client)
    for model_id, reason in (
        ("vendor/conflicting", "billing_paid"),
        ("vendor/uncertain", "billing_unverified"),
    ):
        with pytest.raises(RuntimeError, match=f"inference_blocked:{reason}"):
            client.complete(provider, model_id, RouteRequest("hello"))


def test_duplicate_openrouter_rows_cannot_downgrade_paid_to_unknown(monkeypatch):
    entries = [
        {"id": "vendor/mixed", "pricing": {"prompt": "0.5", "completion": "0"}},
        {"id": "vendor/mixed"},
        {"id": "vendor/mixed", "pricing": {"prompt": "0", "completion": "0"}},
    ]

    class FakeHTTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return httpx.Response(
                200, request=httpx.Request("GET", url), json={"data": entries}
            )

    monkeypatch.setattr(httpx, "Client", FakeHTTP)
    spec = next(item for item in BUILTINS if item.id == "openrouter")
    provider = RegisteredProvider(spec, "offline-fixture", RuntimeState())
    ids, prices = ProviderClient().discover_inventory(provider)
    assert ids == ["vendor/mixed"]
    assert prices == {"vendor/mixed": "paid"}
