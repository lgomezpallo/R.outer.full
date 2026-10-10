from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

Capability = Literal["chat", "reasoning", "json", "vision", "tools", "code", "coding", "summarization", "document", "transcription", "speech", "image_generation", "image_editing", "long_context", "fast"]

@dataclass(frozen=True)
class ProviderCredential:
    name: str
    api_key: str

@dataclass(frozen=True)
class RouteRequest:
    task: str
    context: str = ""
    conversation: tuple[tuple[str, str], ...] = ()
    requirements: tuple[str, ...] = ()
    required_capabilities: frozenset[Capability] = frozenset({"chat"})
    preferred_model_class: str | None = None
    timeout_s: float = 45.0
    application_name: str = "unknown"
    decompose: bool | None = None
    max_subtasks: int = 8
    max_strategic_cost: int | None = None

@dataclass
class Decision:
    provider: str
    model: str
    score: float
    reasons: list[str] = field(default_factory=list)

@dataclass
class Attempt:
    provider: str
    model: str
    ok: bool
    latency_ms: int
    error: str | None = None
    phase: str = "execute"

@dataclass
class RouteResponse:
    ok: bool
    text: str | None
    provider: str | None
    model: str | None
    attempts: list[Attempt]
    decisions: list[Decision]
    error: str | None = None
    raw: dict[str, Any] | None = None
