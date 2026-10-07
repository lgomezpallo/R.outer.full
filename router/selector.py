from __future__ import annotations
from .registry import RegisteredProvider
from .types import Decision, RouteRequest

def _score(provider: RegisteredProvider, model, req: RouteRequest) -> Decision | None:
    missing = req.required_capabilities - model.capabilities
    if missing:
        return None
    if not provider.state.available:
        return None
    reasons: list[str] = []
    score = float(model.priority)
    reasons.append(f"model_priority={model.priority}")
    model_state = provider.state.models.get(model.id)
    success_rate = model_state.success_rate if model_state else provider.state.success_rate
    score += success_rate * 25.0
    reasons.append(f"success_rate={success_rate:.2f}")
    latency = model_state.ewma_latency_ms if model_state and model_state.ewma_latency_ms is not None else provider.state.ewma_latency_ms
    if latency is not None:
        latency_bonus = max(0.0, 20.0 - latency / 100.0)
        score += latency_bonus
        reasons.append(f"latency_bonus={latency_bonus:.2f}")
    else:
        score += 10.0
        reasons.append("latency_unknown_bonus=10")
    if model.verified_capabilities.issuperset(req.required_capabilities):
        score += 15.0
        reasons.append("verified_capabilities=match")
    if req.preferred_model_class and model.model_class == req.preferred_model_class:
        score += 30.0
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
