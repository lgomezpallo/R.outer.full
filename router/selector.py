from __future__ import annotations
from .registry import RegisteredProvider
from .types import Decision, RouteRequest

def _provider_health_rank(provider: RegisteredProvider) -> int:
    if not provider.state.available:
        return 0
    if provider.state.health_ok is False:
        return 1
    return 2

def _score(provider: RegisteredProvider, model, req: RouteRequest) -> Decision | None:
    if req.required_capabilities & model.unsupported_capabilities:
        return None
    missing = req.required_capabilities - model.capabilities
    if missing:
        return None
    if not provider.state.available:
        return None

    reasons: list[str] = []
    health_rank = _provider_health_rank(provider)
    model_state = provider.state.models.get(model.id)
    success_rate = model_state.success_rate if model_state else provider.state.success_rate
    latency = (
        model_state.ewma_latency_ms
        if model_state and model_state.ewma_latency_ms is not None
        else provider.state.ewma_latency_ms
    )
    strategic_cost = provider.spec.strategic_cost + model.strategic_cost

    # Health comes first. Among healthy/sufficient candidates, the resource with
    # the lowest strategic cost wins. Performance and administrative priority
    # resolve ties; this prevents burning scarce providers on trivial work.
    score = health_rank * 10000.0
    reasons.append(f"health_rank={health_rank}")
    score -= strategic_cost * 100.0
    reasons.append(f"strategic_cost={strategic_cost}")

    verified = model.verified_capabilities.issuperset(req.required_capabilities)
    if verified:
        score += 500.0
        reasons.append("verified_capabilities=match")
    else:
        reasons.append("verified_capabilities=not_proven")

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
