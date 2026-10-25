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
