from __future__ import annotations
from dataclasses import dataclass
from .types import Capability

@dataclass(frozen=True)
class ModelSpec:
    id: str
    capabilities: frozenset[Capability]
    model_class: str
    priority: int = 50

@dataclass(frozen=True)
class ProviderSpec:
    id: str
    aliases: tuple[str, ...]
    base_url: str
    models: tuple[ModelSpec, ...]

CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="groq",
        aliases=("groq", "groc", "groq ai"),
        base_url="https://api.groq.com/openai/v1",
        models=(
            ModelSpec("openai/gpt-oss-20b", frozenset({"chat","reasoning","json","code"}), "standard", 70),
            ModelSpec("llama-3.3-70b-versatile", frozenset({"chat","reasoning","json","code"}), "strong", 90),
        ),
    ),
    ProviderSpec(
        id="openrouter",
        aliases=("openrouter", "open router", "open-router"),
        base_url="https://openrouter.ai/api/v1",
        models=(
            ModelSpec("openrouter/auto", frozenset({"chat","reasoning","json","code"}), "auto", 80),
        ),
    ),
)
