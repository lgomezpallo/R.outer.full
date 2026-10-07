from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock
from .types import Capability

CAPABILITY_STATUSES = frozenset({"verified", "unsupported", "inconclusive"})

@dataclass(frozen=True)
class CapabilityEvidence:
    capability: str
    status: str
    evidence: str
    checked_at: str

    @classmethod
    def now(cls, capability: str, status: str, evidence: str) -> "CapabilityEvidence":
        if status not in CAPABILITY_STATUSES:
            raise ValueError("invalid_capability_status")
        return cls(capability, status, evidence[:500], datetime.now(timezone.utc).isoformat())

@dataclass(frozen=True)
class ModelSpec:
    id: str
    capabilities: frozenset[Capability]
    model_class: str
    priority: int = 50
    strategic_cost: int = 50
    evidence: tuple[CapabilityEvidence, ...] = ()

    @property
    def verified_capabilities(self) -> frozenset[str]:
        return frozenset(item.capability for item in self.evidence if item.status == "verified")

    @property
    def unsupported_capabilities(self) -> frozenset[str]:
        return frozenset(item.capability for item in self.evidence if item.status == "unsupported")

@dataclass(frozen=True)
class ProviderSpec:
    id: str
    aliases: tuple[str, ...]
    base_url: str
    models: tuple[ModelSpec, ...]
    protocol: str = "openai-compatible"
    priority: int = 50
    strategic_cost: int = 50
    discover_models: bool = True

def _m(model_id: str, caps: set[str], model_class: str, priority: int, cost: int) -> ModelSpec:
    return ModelSpec(model_id, frozenset(caps), model_class, priority, cost)

def infer_discovered_capabilities(model_id: str) -> frozenset[str]:
    name = model_id.lower()
    if any(token in name for token in ("prompt-guard", "safeguard", "moderation", "safety", "rerank", "embed")):
        return frozenset()
    if "whisper" in name or "transcri" in name:
        return frozenset({"transcription"})
    if "orpheus" in name or "tts" in name or "text-to-speech" in name:
        return frozenset({"speech"})
    capabilities = {"chat"}
    if any(token in name for token in ("coder", "code", "codestral")):
        capabilities.add("code")
        capabilities.add("coding")
    if any(token in name for token in ("reason", "qwq", "r1", "gpt-oss")):
        capabilities.add("reasoning")
    return frozenset(capabilities)

# The built-ins are a safe bootstrap. When a configured provider exposes /models,
# discovery enriches this catalog without requiring changes in client applications.
BUILTINS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="groq",
        aliases=("groq", "groc", "groq ai"),
        base_url="https://api.groq.com/openai/v1",
        strategic_cost=10,
        priority=90,
        models=(
            _m("openai/gpt-oss-20b", {"chat","reasoning","json","code","coding"}, "standard", 75, 8),
            _m("openai/gpt-oss-120b", {"chat","reasoning","json","code","coding"}, "strong", 92, 28),
            _m("qwen/qwen3.8-27b", {"chat","reasoning","json","code","coding"}, "standard", 80, 12),
            _m("whisper-large-v3", {"transcription"}, "specialist", 70, 5),
            _m("whisper-large-v3-turbo", {"transcription","fast"}, "specialist", 82, 7),
            _m("canopylabs/orpheus-v1-english", {"speech"}, "specialist", 68, 7),
            _m("canopylabs/orpheus-arabic-saudi", {"speech"}, "specialist", 66, 7),
        ),
    ),
    ProviderSpec(
        id="openrouter",
        aliases=("openrouter", "open router", "open-router"),
        base_url="https://openrouter.ai/api/v1",
        strategic_cost=18,
        priority=80,
        models=(
            _m("openrouter/auto", {"chat","reasoning","json","code","coding"}, "auto", 70, 22),
        ),
    ),
    ProviderSpec(
        id="nvidia",
        aliases=("nvidia", "nvidia nim", "nim"),
        base_url="https://integrate.api.nvidia.com/v1",
        strategic_cost=35,
        priority=65,
        models=(
            _m("meta/llama-3.1-70b-instruct", {"chat","reasoning","json","code","coding"}, "strong", 78, 38),
        ),
    ),
    ProviderSpec(
        id="mistral",
        aliases=("mistral", "mistral ai"),
        base_url="https://api.mistral.ai/v1",
        strategic_cost=30,
        priority=65,
        models=(
            _m("mistral-small-latest", {"chat","reasoning","json","code","coding"}, "standard", 72, 26),
        ),
    ),
    ProviderSpec(
        id="cerebras",
        aliases=("cerebras", "cerebras inference"),
        base_url="https://api.cerebras.ai/v1",
        strategic_cost=45,
        priority=60,
        models=(
            _m("zai-glm-4.7", {"chat","reasoning","json","code","coding"}, "strong", 82, 48),
        ),
    ),
    ProviderSpec(
        id="sambanova",
        aliases=("sambanova", "samba nova", "sambacloud"),
        base_url="https://api.sambanova.ai/v1",
        strategic_cost=48,
        priority=58,
        models=(
            _m("Meta-Llama-3.1-405B-Instruct", {"chat","reasoning","json","code","coding"}, "strong", 84, 52),
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

    def merge_discovered_models(self, provider_id: str, model_ids: list[str]) -> ProviderSpec:
        with self._lock:
            provider = self._providers[provider_id]
            existing = {model.id: model for model in provider.models}
            for model_id in model_ids:
                if model_id not in existing:
                    existing[model_id] = ModelSpec(
                        id=model_id,
                        capabilities=infer_discovered_capabilities(model_id),
                        model_class="unknown",
                        priority=45,
                        strategic_cost=provider.strategic_cost,
                    )
            updated = replace(provider, models=tuple(existing.values()))
            self._providers[provider_id] = updated
            self._persist_unlocked()
            return updated

    def record_capability(
        self,
        provider_id: str,
        model_id: str,
        capability: str,
        status: str,
        evidence: str,
    ) -> ProviderSpec:
        if status not in CAPABILITY_STATUSES:
            raise ValueError("invalid_capability_status")
        with self._lock:
            provider = self._providers[provider_id]
            models = []
            found = False
            for model in provider.models:
                if model.id != model_id:
                    models.append(model)
                    continue
                found = True
                retained = tuple(item for item in model.evidence if item.capability != capability)
                models.append(replace(
                    model,
                    evidence=retained + (CapabilityEvidence.now(capability, status, evidence),),
                ))
            if not found:
                raise KeyError("unknown_model")
            updated = replace(provider, models=tuple(models))
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
                "priority": provider.priority,
                "strategic_cost": provider.strategic_cost,
                "discover_models": provider.discover_models,
                "models": [
                    {
                        "id": model.id,
                        "model_class": model.model_class,
                        "priority": model.priority,
                        "strategic_cost": model.strategic_cost,
                        "declared_capabilities": sorted(model.capabilities),
                        "verified_capabilities": sorted(model.verified_capabilities),
                        "unsupported_capabilities": sorted(model.unsupported_capabilities),
                        "evidence": [item.__dict__ for item in model.evidence],
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
            models = []
            for model in item.get("models", []):
                evidence = tuple(
                    CapabilityEvidence(
                        capability=e["capability"],
                        status=e["status"],
                        evidence=e.get("evidence", ""),
                        checked_at=e.get("checked_at", ""),
                    )
                    for e in model.get("evidence", [])
                    if e.get("status") in CAPABILITY_STATUSES
                )
                # Backward compatible with the previous catalog format.
                for capability in model.get("verified_capabilities", []):
                    if not any(e.capability == capability for e in evidence):
                        evidence += (CapabilityEvidence.now(capability, "verified", "migrated_from_v1"),)
                models.append(ModelSpec(
                    id=model["id"],
                    capabilities=frozenset(model.get("declared_capabilities", [])),
                    model_class=model.get("model_class", "unknown"),
                    priority=int(model.get("priority", 50)),
                    strategic_cost=int(model.get("strategic_cost", item.get("strategic_cost", 50))),
                    evidence=evidence,
                ))
            provider = ProviderSpec(
                id=item["id"],
                aliases=tuple(item.get("aliases", [])),
                base_url=item["base_url"],
                protocol=item.get("protocol", "openai-compatible"),
                priority=int(item.get("priority", 50)),
                strategic_cost=int(item.get("strategic_cost", 50)),
                discover_models=bool(item.get("discover_models", True)),
                models=tuple(models),
            )
            providers[provider.id] = provider
        if providers:
            with self._lock:
                self._providers.update(providers)

    def _persist_unlocked(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        # snapshot() re-enters the RLock safely.
        payload = {"version": 2, "providers": self.snapshot()}
        tmp = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.storage_path)

DEFAULT_CATALOG = ProviderCatalog()
CATALOG = DEFAULT_CATALOG.all()
