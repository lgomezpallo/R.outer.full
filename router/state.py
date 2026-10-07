from __future__ import annotations
from dataclasses import dataclass, field
from time import time

@dataclass
class ModelRuntimeState:
    success_count: int = 0
    failure_count: int = 0
    ewma_latency_ms: float | None = None
    last_error: str | None = None

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 1.0

    def mark_success(self, latency_ms: int) -> None:
        self.success_count += 1
        self.last_error = None
        self.ewma_latency_ms = float(latency_ms) if self.ewma_latency_ms is None else self.ewma_latency_ms * 0.7 + latency_ms * 0.3

    def mark_failure(self, error: str) -> None:
        self.failure_count += 1
        self.last_error = error

@dataclass
class RuntimeState:
    success_count: int = 0
    failure_count: int = 0
    ewma_latency_ms: float | None = None
    cooldown_until: float = 0.0
    last_error: str | None = None
    health_ok: bool | None = None
    models: dict[str, ModelRuntimeState] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 1.0

    @property
    def available(self) -> bool:
        return time() >= self.cooldown_until

    @property
    def health_status(self) -> str:
        if not self.available:
            return "unavailable"
        if self.health_ok is False or (self.failure_count >= 2 and self.success_rate < 0.7):
            return "degraded"
        return "available"

    def model(self, model_id: str) -> ModelRuntimeState:
        return self.models.setdefault(model_id, ModelRuntimeState())

    def mark_success(self, model_id: str, latency_ms: int) -> None:
        self.success_count += 1
        self.last_error = None
        self.health_ok = True
        self.cooldown_until = 0.0
        self.ewma_latency_ms = float(latency_ms) if self.ewma_latency_ms is None else self.ewma_latency_ms * 0.7 + latency_ms * 0.3
        self.model(model_id).mark_success(latency_ms)

    def mark_failure(self, model_id: str, error: str, cooldown_s: float = 15.0) -> None:
        self.failure_count += 1
        self.last_error = error
        self.health_ok = False
        self.cooldown_until = time() + max(0.0, cooldown_s)
        self.model(model_id).mark_failure(error)
