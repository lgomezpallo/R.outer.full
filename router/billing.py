"""Conservative per-model billing policy for catalog discovery.

A model appearing in a provider's /models response is not evidence that using
it is free. Keep NVIDIA's *previously routable* set unchanged while making
the entire discovered inventory visible to diagnostics. This is an allowlist
of prior routing policy, NOT a guarantee of current provider pricing.
"""
from __future__ import annotations

NVIDIA_ROUTING_ALLOWLIST = frozenset({
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "google/gemma-4-31b-it",
    "moonshotai/kimi-k3",
    "meta/llama-3.2-11b-vision-instruct",
    "meta/llama-3.2-90b-vision-instruct",
    "google/diffusiongemma-26b-a4b-it",
})

# These Cloudflare models explicitly require paid billing or prepaid credits.
# Other Workers AI models are NOT automatically free: Cloudflare's daily
# neuron allocation depends on account plan and remaining quota.
CLOUDFLARE_PAID_ONLY = frozenset({
    "@cf/moonshotai/kimi-k2.6",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/zai-org/glm-5.2",
    "@cf/zai-org/glm-5.3",
    "@cf/zai-org/glm-5.3-flash",
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
})


# A catalog file is mutable. Its provider id alone must never authorize a
# network destination: a spoofed Groq/OpenRouter URL could send credentials or
# inference to an arbitrary, potentially billable service.
def billing_endpoint_ineligibility_reason(provider_id: str, base_url: str, protocol: str) -> str | None:
    from urllib.parse import urlsplit
    import re

    if protocol != "openai-compatible":
        return "billing_unverified_endpoint"
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return "billing_unverified_endpoint"
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.netloc != parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        return "billing_unverified_endpoint"
    expected = {
        "groq": ("api.groq.com", "/openai/v1"),
        "nvidia": ("integrate.api.nvidia.com", "/v1"),
        "openrouter": ("openrouter.ai", "/api/v1"),
    }
    if provider_id in expected:
        hostname, path = expected[provider_id]
        if parsed.hostname == hostname and parsed.path.rstrip("/") == path:
            return None
    elif provider_id == "cloudflare":
        if parsed.hostname == "api.cloudflare.com" and re.fullmatch(
            r"/client/v4/accounts/[A-Za-z0-9_-]+/ai/?", parsed.path
        ):
            return None
    return "billing_unverified_endpoint"


def account_billing_ineligibility_reason(provider_id: str) -> str | None:
    """Deny inference until a non-billable ACCOUNT state can be proven.

    Zero catalog prices, a free-tier label, a previous allowlist, or a local
    environment variable cannot prove that provider-side overages are off.
    No provider currently exposes verified, immutable account-level evidence
    to this router. There is deliberately no production opt-in switch.
    Discovery remains permitted after endpoint validation (no inference).
    """
    return "billing_unverified_account"
