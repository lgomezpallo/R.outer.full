"""No inference may bypass the last-mile billing guard.

These tests are fully offline. A network client that gets constructed means
the safety check ran too late and the test must fail.
"""
from dataclasses import replace

import httpx
import pytest

from router.catalog import BUILTINS, ModelSpec, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.state import RuntimeState
from router.types import RouteRequest


def _registered(provider_id: str, model_id: str, billing_status: str = "unknown"):
    if provider_id == "cloudflare":
        spec = ProviderSpec(
            id="cloudflare",
            aliases=("cloudflare",),
            base_url="https://api.cloudflare.com/client/v4/accounts/test/ai",
            models=(),
        )
    else:
        spec = next(item for item in BUILTINS if item.id == provider_id)
    model = ModelSpec(model_id, frozenset({"chat", "vision"}), "unknown", billing_status=billing_status)
    return RegisteredProvider(replace(spec, models=(model,)), "fake-key", RuntimeState())


@pytest.mark.parametrize(
    "provider_id,model_id,billing_status",
    [
        ("openrouter", "vendor/paid", "paid"),
        ("openrouter", "vendor/unknown", "unknown"),
        ("openrouter", "openrouter/free", "unknown"),
        ("nvidia", "vendor/not-verified", "unknown"),
        ("mistral", "mistral-small-latest", "free"),
    ],
)
def test_direct_inference_and_probe_transports_never_touch_network(
    monkeypatch, provider_id, model_id, billing_status
):
    provider = _registered(provider_id, model_id, billing_status)
    network_calls = []

    def forbid_http(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("network_request_before_billing_gate")

    monkeypatch.setattr(httpx, "Client", forbid_http)
    client = ProviderClient()
    request = RouteRequest("hola", required_capabilities=frozenset({"chat"}), decompose=False)
    transports = (
        lambda: client.complete(provider, model_id, request),
        lambda: client.probe_capability(provider, model_id, "vision"),
        lambda: client._openai_compatible(provider, model_id, request),
        lambda: client._anthropic(provider, model_id, request),
        lambda: client._gemini(provider, model_id, request),
    )
    for invoke in transports:
        with pytest.raises(RuntimeError, match="inference_blocked:billing_"):
            invoke()
    assert network_calls == []


def test_uncataloged_model_rejected_before_http(monkeypatch):
    provider = _registered("groq", "openai/gpt-oss-20b")
    client = ProviderClient()
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("network_request"))
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_model"):
        client.complete(provider, "injected/not-cataloged", RouteRequest("hola"))

def test_free_cloudflare_passes_model_gate_but_offline_flag_still_blocks(monkeypatch):
    provider = _registered("cloudflare", "@cf/vendor/free", "free")
    ProviderClient._require_billing_eligible(provider, "@cf/vendor/free")
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    with pytest.raises(RuntimeError, match="inference_blocked:offline_only"):
        ProviderClient._require_billing_eligible(provider, "@cf/vendor/free")
