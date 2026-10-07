from __future__ import annotations
from dataclasses import dataclass, replace
from threading import RLock
from .types import Capability

@dataclass(frozen=True)
class ModelSpec:
    id: str
    capabilities: frozenset[Capability]
    model_class: str
    priority: int = 50
    verified_capabilities: frozenset[Capability] = frozenset()

@dataclass(frozen=True)
class ProviderSpec:
    id: str
    aliases: tuple[str, ...]
    base_url: str
    models: tuple[ModelSpec, ...]
    protocol: str = "openai-compatible"

BUILTINS: tuple[ProviderSpec, ...] = (
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

class ProviderCatalog:
    def __init__(self, providers: tuple[ProviderSpec, ...] = BUILTINS) -> None:
        self._providers = {provider.id: provider for provider in providers}
        self._lock = RLock()

    def all(self) -> tuple[ProviderSpec, ...]:
        with self._lock:
            return tuple(self._providers.values())

    def get(self, provider_id: str) -> ProviderSpec | None:
        with self._lock:
            return self._providers.get(provider_id)

    def put(self, provider: ProviderSpec) -> ProviderSpec:
        with self._lock:
            self._providers[provider.id] = provider
        return provider

    def verify_capabilities(self, provider_id: str, model_id: str, capabilities: frozenset[Capability]) -> ProviderSpec:
        with self._lock:
            provider = self._providers[provider_id]
            models = tuple(
                replace(model, verified_capabilities=capabilities)
                if model.id == model_id else model
                for model in provider.models
            )
            updated = replace(provider, models=models)
            self._providers[provider_id] = updated
            return updated

    def snapshot(self) -> list[dict]:
        return [
            {
                "id": provider.id,
                "aliases": list(provider.aliases),
                "base_url": provider.base_url,
                "protocol": provider.protocol,
                "models": [
                    {
                        "id": model.id,
                        "model_class": model.model_class,
                        "priority": model.priority,
                        "declared_capabilities": sorted(model.capabilities),
                        "verified_capabilities": sorted(model.verified_capabilities),
                    }
                    for model in provider.models
                ],
            }
            for provider in self.all()
        ]

DEFAULT_CATALOG = ProviderCatalog()
CATALOG = DEFAULT_CATALOG.all()
