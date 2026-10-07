from __future__ import annotations
from dataclasses import dataclass, replace
import json
from pathlib import Path
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
    def __init__(
        self,
        providers: tuple[ProviderSpec, ...] = BUILTINS,
        storage_path: str | Path | None = None,
    ) -> None:
        self._providers = {provider.id: provider for provider in providers}
        self._lock = RLock()
        self.storage_path = Path(storage_path) if storage_path else None
        if self.storage_path and self.storage_path.exists():
            self.load()

    def all(self) -> tuple[ProviderSpec, ...]:
        with self._lock:
            return tuple(self._providers.values())

    def get(self, provider_id: str) -> ProviderSpec | None:
        with self._lock:
            return self._providers.get(provider_id)

    def put(self, provider: ProviderSpec) -> ProviderSpec:
        with self._lock:
            self._providers[provider.id] = provider
            self._persist_unlocked()
        return provider

    def verify_capabilities(
        self,
        provider_id: str,
        model_id: str,
        capabilities: frozenset[Capability],
    ) -> ProviderSpec:
        with self._lock:
            provider = self._providers[provider_id]
            models = tuple(
                replace(model, verified_capabilities=capabilities)
                if model.id == model_id else model
                for model in provider.models
            )
            updated = replace(provider, models=models)
            self._providers[provider_id] = updated
            self._persist_unlocked()
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

    def save(self) -> None:
        with self._lock:
            self._persist_unlocked()

    def load(self) -> None:
        if not self.storage_path:
            return
        raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        providers: dict[str, ProviderSpec] = {}
        for item in raw.get("providers", []):
            models = tuple(
                ModelSpec(
                    id=model["id"],
                    capabilities=frozenset(model.get("declared_capabilities", [])),
                    model_class=model.get("model_class", "standard"),
                    priority=int(model.get("priority", 50)),
                    verified_capabilities=frozenset(model.get("verified_capabilities", [])),
                )
                for model in item.get("models", [])
            )
            provider = ProviderSpec(
                id=item["id"],
                aliases=tuple(item.get("aliases", [])),
                base_url=item["base_url"],
                protocol=item.get("protocol", "openai-compatible"),
                models=models,
            )
            providers[provider.id] = provider
        if providers:
            with self._lock:
                self._providers.update(providers)

    def _persist_unlocked(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "providers": self.snapshot()}
        tmp = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.storage_path)

DEFAULT_CATALOG = ProviderCatalog()
CATALOG = DEFAULT_CATALOG.all()
