"""Offline regression: URL parser normalization cannot authorize credential-bearing HTTP.

No API keys, network calls, or billing settings are accessed. An HTTP client
construction is treated as a test failure.
"""
from dataclasses import replace

import httpx
import pytest

from router.billing import billing_endpoint_ineligibility_reason
from router.catalog import BUILTINS, ModelSpec, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.state import RuntimeState
from router.types import RouteRequest


pytestmark = pytest.mark.strict_billing

ENDPOINTS = (
    ("groq", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "google/gemma-4-31b-it"),
    ("openrouter", "https://openrouter.ai/api/v1", "vendor/free-zero"),
    ("cloudflare", "https://api.cloudflare.com/client/v4/accounts/test/ai", "@cf/test/model"),
)


def _provider(provider_id, base_url, model_id):
    if provider_id == "cloudflare":
        spec = ProviderSpec(provider_id, ("cloudflare",), base_url, ())
    else:
        spec = next(item for item in BUILTINS if item.id == provider_id)
        spec = replace(spec, base_url=base_url)
    model = ModelSpec(model_id, frozenset({"chat", "vision"}), "standard", billing_status="free")
    return RegisteredProvider(replace(spec, models=(model,)), "offline-fake-key", RuntimeState())


@pytest.mark.parametrize("provider_id,base,model_id", ENDPOINTS)
@pytest.mark.parametrize("mutation", [
    "leading_space", "leading_tab", "leading_newline", "trailing_cr",
    "trailing_newline", "embedded_tab", "embedded_newline", "unicode_space",
])
def test_noncanonical_url_denied_before_any_http(monkeypatch, provider_id, base, model_id, mutation):
    invalid = {
        "leading_space": " " + base,
        "leading_tab": "\t" + base,
        "leading_newline": "\n" + base,
        "trailing_cr": base + "\r",
        "trailing_newline": base + "\n",
        "embedded_tab": base.replace("https://", "https://\t", 1),
        "embedded_newline": base.replace("https://", "https://\n", 1),
        "unicode_space": base + "\u00a0",
    }[mutation]
    assert billing_endpoint_ineligibility_reason(provider_id, invalid, "openai-compatible") == "billing_unverified_endpoint"

    monkeypatch.setattr(
        httpx, "Client",
        lambda *args, **kwargs: pytest.fail("HTTP client constructed for invalid endpoint"),
    )
    provider = _provider(provider_id, invalid, model_id)
    client = ProviderClient()
    with pytest.raises(RuntimeError, match="inventory_blocked:billing_unverified_endpoint"):
        client.discover_inventory(provider)
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_endpoint"):
        client.complete(provider, model_id, RouteRequest("hello"))
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_endpoint"):
        client.probe_capability(provider, model_id, "vision")


@pytest.mark.parametrize("provider_id,base,model_id", ENDPOINTS)
def test_canonical_endpoints_pass_only_endpoint_validation(provider_id, base, model_id):
    # Passing the destination check does NOT authorize inference or account billing.
    assert billing_endpoint_ineligibility_reason(provider_id, base, "openai-compatible") is None
