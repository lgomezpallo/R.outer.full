from __future__ import annotations
from .registry import RegisteredProvider
from .types import Decision, RouteRequest

SPECIALIZED_CAPABILITIES = frozenset({
    "vision", "code", "coding", "reasoning", "document",
    "transcription", "speech", "image_generation", "image_editing", "long_context",
})
GENERIC_EXTRAS = frozenset({"chat", "json", "tools", "fast", "summarization"})

def _provider_health_rank(provider: RegisteredProvider) -> int:
    if not provider.state.available:
        return 0
    if provider.state.health_status == "degraded":
        return 1
    return 2

def _verification_tier(model, required: frozenset[str]) -> int:
    if required & model.unsupported_capabilities:
        return 9
    verified = model.verified_capabilities
    if required.issubset(verified):
        return 0
    inconclusive = {
        item.capability for item in model.evidence if item.status == "inconclusive"
    }
    if required & inconclusive:
        return 2
    return 1

def _specialization_penalty(model, required: frozenset[str]) -> int:
    extras = set(model.capabilities) - set(required) - set(GENERIC_EXTRAS)
    return sum(1 for item in extras if item in SPECIALIZED_CAPABILITIES)

def _score(provider: RegisteredProvider, model, req: RouteRequest) -> Decision | None:
    # Discovered /models endpoints include language-specific and experimental
    # architectures that should not be treated as general-purpose chat models.
    # Keep Arabic-specialized models eligible when the task is in Arabic.
    model_name = model.id.casefold()
    if "diffusiongemma" in model_name and "chat" in req.required_capabilities:
        return None
    if "allam-" in model_name and "chat" in req.required_capabilities:
        task_text = req.task + " " + req.context
        if not any("\u0600" <= char <= "\u06ff" for char in task_text):
            return None
    if req.required_capabilities & model.unsupported_capabilities:
        return None
    effective_capabilities = model.capabilities | model.verified_capabilities
    if req.required_capabilities - effective_capabilities:
        return None
    if not provider.state.available:
        return None

    strategic_cost = provider.spec.strategic_cost + model.strategic_cost
    if req.max_strategic_cost is not None and strategic_cost > req.max_strategic_cost:
        return None

    health_rank = _provider_health_rank(provider)
    verification_tier = _verification_tier(model, req.required_capabilities)
    specialization_penalty = _specialization_penalty(model, req.required_capabilities)
    model_state = provider.state.models.get(model.id)
    success_rate = model_state.success_rate if model_state else provider.state.success_rate
    latency = (
        model_state.ewma_latency_ms
        if model_state and model_state.ewma_latency_ms is not None
        else provider.state.ewma_latency_ms
    )

    reasons: list[str] = []
    score = health_rank * 100000.0
    reasons.append(f"health_rank={health_rank}")

    score -= verification_tier * 10000.0
    reasons.append(f"verification_tier={verification_tier}")

    score -= strategic_cost * 100.0
    reasons.append(f"strategic_cost={strategic_cost}")

    score -= specialization_penalty * 300.0
    reasons.append(f"specialization_penalty={specialization_penalty}")

    score += success_rate * 100.0
    reasons.append(f"success_rate={success_rate:.2f}")

    if latency is not None:
        latency_bonus = max(0.0, 50.0 - latency / 100.0)
        score += latency_bonus
        reasons.append(f"latency_bonus={latency_bonus:.2f}")
    else:
        score += 10.0
        reasons.append("latency_unknown_bonus=10")

    score += model.priority + provider.spec.priority
    reasons.append(f"priority={model.priority + provider.spec.priority}")

    if "fast" in model.capabilities and req.required_capabilities == frozenset({"chat"}):
        score += 25.0
        reasons.append("fast_chat_bonus=25")

    if req.preferred_model_class and model.model_class == req.preferred_model_class:
        score += 250.0
        reasons.append("preferred_model_class=match")

    return Decision(provider.spec.id, model.id, score, reasons)

def rank(providers: list[RegisteredProvider], req: RouteRequest) -> list[Decision]:
    decisions: list[Decision] = []
    for provider in providers:
        for model in provider.spec.models:
            decision = _score(provider, model, req)
            if decision:
                decisions.append(decision)
    return sorted(decisions, key=lambda d: d.score, reverse=True)
