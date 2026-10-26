"""Regression: never authorize free pricing from a truncated provider inventory.

All HTTP responses are in-memory mocks; no inference, billing, or external calls.
"""
import httpx
import pytest

from router.catalog import BUILTINS
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.state import RuntimeState
from router.types import RouteRequest


pytestmark = pytest.mark.strict_billing


def _discover_with_mock(monkeypatch, rows):
    calls = {"get": 0, "post": 0}

    class OfflineClient:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("trust_env") is False
            assert kwargs.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls["get"] += 1
            assert url == "https://openrouter.ai/api/v1/models"
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": rows},
            )

        def post(self, *args, **kwargs):
            calls["post"] += 1
            raise AssertionError("real_or_mock_inference_must_not_be_sent")

    monkeypatch.setattr("router.client.httpx.Client", OfflineClient)
    spec = next(p for p in BUILTINS if p.id == "openrouter")
    provider = RegisteredProvider(spec, "offline-only", RuntimeState())
    return provider, calls


@pytest.mark.parametrize("reverse", [False, True])
def test_paid_duplicate_beyond_previous_2000_row_cutoff_is_never_free(monkeypatch, reverse):
    free = {"id": "vendor/duplicate", "pricing": {"prompt": "0", "completion": "0"}}
    paid = {"id": "vendor/duplicate", "pricing": {"prompt": "0.01", "completion": "0"}}
    fillers = [
        {"id": f"vendor/filler-{i}", "pricing": {"prompt": "0", "completion": "0"}}
        for i in range(1999)
    ]
    rows = ([paid] + fillers + [free]) if reverse else ([free] + fillers + [paid])
    assert len(rows) == 2001
    provider, calls = _discover_with_mock(monkeypatch, rows)
    ids, statuses = ProviderClient().discover_inventory(provider)
    assert len(ids) == 2000
    assert ids.count("vendor/duplicate") == 1
    assert statuses["vendor/duplicate"] == "paid"
    assert calls == {"get": 1, "post": 0}
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_model"):
        ProviderClient().complete(provider, "vendor/duplicate", RouteRequest("hola"))
    assert calls["post"] == 0


def test_excessively_large_inventory_is_rejected_not_partially_authorized(monkeypatch):
    rows = [
        {"id": f"vendor/free-{i}", "pricing": {"prompt": "0", "completion": "0"}}
        for i in range(10_001)
    ]
    provider, calls = _discover_with_mock(monkeypatch, rows)
    with pytest.raises(ValueError, match="provider_inventory_too_large"):
        ProviderClient().discover_inventory(provider)
    assert calls == {"get": 1, "post": 0}


def test_maximum_bounded_inventory_is_processed_in_full(monkeypatch):
    rows = [
        {"id": f"vendor/free-{i}", "pricing": {"prompt": "0", "completion": "0"}}
        for i in range(9_999)
    ]
    rows.append({"id": "vendor/last-paid", "pricing": {"prompt": "0.02", "completion": "0"}})
    provider, calls = _discover_with_mock(monkeypatch, rows)
    ids, statuses = ProviderClient().discover_inventory(provider)
    assert len(ids) == 10_000
    assert statuses["vendor/last-paid"] == "paid"
    assert calls == {"get": 1, "post": 0}
