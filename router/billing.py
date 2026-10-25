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
