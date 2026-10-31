"""Offline guard for credential-bearing catalog discovery.

A catalog URL is mutable, so GET /models must validate its destination before
it can construct an HTTP client or transmit the configured provider token.
"""
from dataclasses import replace

import httpx
import pytest

from router.catalog import BUILTINS, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.state import RuntimeState


@pytest.mark.parametrize(
    "provider_id,url",
    [
        ("groq", "https://api.groq.com.evil.invalid/openai/v1"),
        ("groq", "https://api.groq.com:443/openai/v1"),
        ("groq", "http://api.groq.com/openai/v1"),
        ("nvidia", "https://integrate.api.nvidia.com/v1/other"),
        ("openrouter", "https://openrouter.ai.evil.invalid/api/v1"),
        ("cloudflare", "https://api.cloudflare.com/client/v4/accounts/a/ai/other"),
        ("unknown", "https://api.groq.com/openai/v1"),
    ],
)
def test_inventory_and_compatibility_discovery_reject_untrusted_origin_without_http(
    monkeypatch, provider_id, url
):
    base = next((item for item in BUILTINS if item.id == provider_id), None)
    if base is None:
        base = ProviderSpec(provider_id, (provider_id,), url, models=())
    provider = RegisteredProvider(replace(base, base_url=url), "never-send-key", RuntimeState())
    network_calls = []

    def forbidden_client(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("discovery_http_before_origin_validation")

    monkeypatch.setattr(httpx, "Client", forbidden_client)
    for discover in (ProviderClient().discover_inventory, ProviderClient().discover_models):
        with pytest.raises(RuntimeError, match="inventory_blocked:billing_unverified_endpoint"):
            discover(provider)
    assert network_calls == []


def test_canonical_groq_inventory_can_be_read_with_mock_http(monkeypatch):
    base = next(item for item in BUILTINS if item.id == "groq")
    provider = RegisteredProvider(base, "fake-key", RuntimeState())
    observed = []

    class MockClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            observed.append((url, headers["Authorization"]))
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": [{"id": "openai/gpt-oss-20b"}]},
            )

    monkeypatch.setattr(httpx, "Client", MockClient)
    models, billing = ProviderClient().discover_inventory(provider)
    assert models == ["openai/gpt-oss-20b"]
    assert billing == {}
    assert observed == [("https://api.groq.com/openai/v1/models", "Bearer fake-key")]
