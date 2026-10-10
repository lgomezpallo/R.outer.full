from __future__ import annotations
from .catalog import infer_discovered_capabilities
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
    extras = (set(model.capabilities) | set(infer_discovered_capabilities(model.id))) - set(required) - set(GENERIC_EXTRAS)
    return sum(1 for item in extras if item in SPECIALIZED_CAPABILITIES)

def _score(provider: RegisteredProvider, model, req: RouteRequest, scarcity: dict[str, int] | None = None) -> Decision | None:
    # Discovered /models endpoints include language-specific and experimental
    # architectures that should not be treated as general-purpose chat models.
    # Keep Arabic-specialized models eligible when the task is in Arabic.
    model_name = model.id.casefold()
    if "chat" in req.required_capabilities and not infer_discovered_capabilities(model.id) and "chat" not in model.verified_capabilities:
        return None
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
    # Preserve specialized capabilities only when the current task does not need them.
    # Smaller pools incur larger penalties; declared availability is provisional.
    scarce_extras = (set(model.capabilities) | set(model.verified_capabilities) | set(infer_discovered_capabilities(model.id))) - set(req.required_capabilities) - set(GENERIC_EXTRAS)
    scarcity_penalty = sum(min(2000, 8000 // max(1, (scarcity or {}).get(cap, 1))) for cap in scarce_extras if cap in SPECIALIZED_CAPABILITIES)
    model_state = provider.state.models.get(model.id)
    samples = (model_state.success_count + model_state.failure_count) if model_state else 0
    successes = model_state.success_count if model_state else 0
    latency = model_state.ewma_latency_ms if model_state else None

    # Never pretend that an untested model has a measured 100% success rate.
    # Blend observations with a neutral prior; confidence grows gradually.
    observed_reliability = (successes + 2) / (samples + 4)
    reliability_adjustment = (observed_reliability - 0.5) * 3600.0
    latency_adjustment = -min(750.0, latency / 12.0) if latency is not None else 0.0

    reasons: list[str] = []
    score = health_rank * 2500.0
    reasons.append(f"health_rank={health_rank}")

    evidence_adjustment = {0: 1500.0, 1: 0.0, 2: -350.0, 9: -10000.0}[verification_tier]
    score += evidence_adjustment
    reasons.append(f"verification_tier={verification_tier}")

    score -= strategic_cost * 12.0
    reasons.append(f"strategic_cost={strategic_cost}")

    score -= specialization_penalty * 200.0
    score -= scarcity_penalty * 0.4
    reasons.append(f"scarcity_penalty={scarcity_penalty}")
    reasons.append(f"specialization_penalty={specialization_penalty}")

    score += reliability_adjustment
    reasons.append(f"observed_samples={samples}")
    reasons.append(f"smoothed_reliability={observed_reliability:.3f}")
    score += latency_adjustment
    reasons.append(f"observed_latency_ms={latency if latency is not None else 'unknown'}")

    score += (model.priority + provider.spec.priority) * 2.0
    reasons.append(f"priority={model.priority + provider.spec.priority}")

    if "fast" in model.capabilities and req.required_capabilities == frozenset({"chat"}):
        score += 80.0
        reasons.append("fast_chat_bonus=80")

    if req.preferred_model_class and model.model_class == req.preferred_model_class:
        score += 250.0
        reasons.append("preferred_model_class=match")

    return Decision(provider.spec.id, model.id, score, reasons)

def rank(providers: list[RegisteredProvider], req: RouteRequest) -> list[Decision]:
    decisions: list[Decision] = []
    scarcity: dict[str, int] = {}
    for provider in providers:
        if not provider.state.available:
            continue
        for model in provider.spec.models:
            for capability in (model.capabilities | model.verified_capabilities | infer_discovered_capabilities(model.id)) - model.unsupported_capabilities:
                scarcity[capability] = scarcity.get(capability, 0) + 1
    for provider in providers:
        for model in provider.spec.models:
            decision = _score(provider, model, req, scarcity)
            if decision:
                decisions.append(decision)
    return sorted(decisions, key=lambda d: d.score, reverse=True)
