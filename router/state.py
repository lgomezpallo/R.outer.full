from __future__ import annotations
from dataclasses import dataclass
from time import monotonic

@dataclass
class RuntimeState:
    success_count: int = 0
    failure_count: int = 0
    ewma_latency_ms: float | None = None
    cooldown_until: float = 0.0
    last_error: str | None = None

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 1.0

    @property
    def available(self) -> bool:
        return monotonic() >= self.cooldown_until

    def mark_success(self, latency_ms: int) -> None:
        self.success_count += 1
        self.last_error = None
        self.ewma_latency_ms = float(latency_ms) if self.ewma_latency_ms is None else self.ewma_latency_ms * 0.7 + latency_ms * 0.3

    def mark_failure(self, error: str, cooldown_s: float = 15.0) -> None:
        self.failure_count += 1
        self.last_error = error
        self.cooldown_until = monotonic() + cooldown_s
