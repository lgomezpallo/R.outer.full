"""Conservative per-model billing policy for catalog discovery.

A model appearing in a provider's /models response is not evidence that using
it is free. Keep NVIDIA's *previously routable* set unchanged while making
the entire discovered inventory visible to diagnostics. This is an allowlist
of prior routing policy, NOT a guarantee of current provider pricing.
"""
from __future__ import annotations
import os

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
    # urlsplit silently removes leading C0 whitespace and strips CR/LF/TAB
    # anywhere in a URL. Do not authorize a normalized URL different from the
    # exact configuration later passed to the HTTP client with credentials.
    if not isinstance(base_url, str) or any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in base_url
    ):
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
    """Account billing audits are advisory, not a Router execution prerequisite.

    Model-level verified-free eligibility and endpoint validation remain
    independent mandatory protections. This does not control provider charges.
    """
    return None


def offline_only() -> bool:
    """Opt-in hard stop for outbound provider and search traffic during offline release validation."""
    return os.getenv("ROUTER_OFFLINE_ONLY", "").strip() == "1"
