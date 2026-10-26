"""Offline regression tests: a trusted provider id cannot redirect billing to a foreign host."""
from dataclasses import replace

import httpx
import pytest

from router.billing import billing_endpoint_ineligibility_reason
from router.catalog import BUILTINS, ModelSpec, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.selector import billing_ineligibility_reason, rank
from router.state import RuntimeState
from router.types import RouteRequest


@pytest.mark.parametrize(
    "provider_id,url,protocol",
    [
        ("groq", "https://api.groq.com/openai/v1", "openai-compatible"),
        ("nvidia", "https://integrate.api.nvidia.com/v1", "openai-compatible"),
        ("openrouter", "https://openrouter.ai/api/v1", "openai-compatible"),
        ("cloudflare", "https://api.cloudflare.com/client/v4/accounts/abc123/ai", "openai-compatible"),
    ],
)
def test_canonical_provider_origins_are_recognized(provider_id, url, protocol):
    # A canonical endpoint is necessary, but NOT proof of account-level free billing.
    assert billing_endpoint_ineligibility_reason(provider_id, url, protocol) is None


@pytest.mark.parametrize(
    "provider_id,url,protocol",
    [
        ("groq", "https://api.groq.com.evil.invalid/openai/v1", "openai-compatible"),
        ("groq", "http://api.groq.com/openai/v1", "openai-compatible"),
        ("groq", "https://api.groq.com:443/openai/v1", "openai-compatible"),
        ("groq", "https://attacker@api.groq.com/openai/v1", "openai-compatible"),
        ("groq", "https://api.groq.com/openai/v1?bill=1", "openai-compatible"),
        ("groq", "https://api.groq.com/openai/v1", "anthropic"),
        ("nvidia", "https://integrate.api.nvidia.com/v1/other", "openai-compatible"),
        ("openrouter", "https://openrouter.ai.evil.invalid/api/v1", "openai-compatible"),
        ("cloudflare", "https://api.cloudflare.com/client/v4/accounts/abc123/ai/other", "openai-compatible"),
        ("cloudflare", "https://api.cloudflare.com/client/v4/accounts//ai", "openai-compatible"),
        ("mistral", "https://api.groq.com/openai/v1", "openai-compatible"),
    ],
)
def test_forged_endpoints_fail_closed_before_any_network(monkeypatch, provider_id, url, protocol):
    spec = next((p for p in BUILTINS if p.id == provider_id), None)
    if spec is None:
        spec = ProviderSpec(provider_id, (provider_id,), url, models=())
    model = ModelSpec("openai/gpt-oss-20b", frozenset({"chat"}), "standard", billing_status="free")
    provider = RegisteredProvider(
        replace(spec, base_url=url, protocol=protocol, models=(model,)), "fake-key", RuntimeState()
    )
    assert billing_endpoint_ineligibility_reason(provider_id, url, protocol) == "billing_unverified_endpoint"
    assert billing_ineligibility_reason(provider, model) == "billing_unverified_endpoint"
    assert rank([provider], RouteRequest("hola")) == []

    def no_network(*args, **kwargs):
        pytest.fail("an unverified endpoint constructed an HTTP client")

    monkeypatch.setattr(httpx, "Client", no_network)
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_endpoint"):
        ProviderClient().complete(provider, model.id, RouteRequest("hola"))
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_endpoint"):
        ProviderClient().probe_capability(provider, model.id, "vision")
