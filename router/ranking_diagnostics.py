"""Explainable, strictly offline provider+model ranking preview.

Uses the production selector without bypassing economic authorization. A
blocked model is counted as blocked, never assigned a fictitious usable score.
"""
from __future__ import annotations

from .capability_evidence import _dimension
from .selector import ineligibility_reason, rank
from .task_requirements import analyze_task
from .types import RouteRequest


def ranking_report(
    providers,
    task: str,
    *,
    requirements: tuple[str, ...] = (),
    required_capabilities: frozenset[str] = frozenset({"chat"}),
    images_present: bool = False,
    max_strategic_cost: int | None = None,
    limit: int = 100,
) -> dict:
    """Read local models and observed runtime metrics; zero provider calls.

    Presentation limit does not constrain the candidate pool or favor providers.
    Quality is deliberately absent: API success is not answer quality.
    """
    if not 1 <= limit <= 500:
        raise ValueError("invalid_preview_limit")
    analysis = analyze_task(
        task, requirements=requirements,
        required_capabilities=required_capabilities,
        images_present=images_present,
    )
    if analysis["invalid_explicit_capabilities"] or analysis["unparsed_requirements"]:
        raise ValueError("invalid_explicit_requirements")
    req = RouteRequest(
        task=task,
        required_capabilities=frozenset(analysis["explicit_required_capabilities"]),
        max_strategic_cost=max_strategic_cost,
    )
    provider_rows = []
    by_key = {}
    for provider in providers:
        excluded = {}
        for model in provider.spec.models:
            reason = ineligibility_reason(provider, model, req)
            if reason is not None:
                excluded[reason] = excluded.get(reason, 0) + 1
            by_key[(provider.spec.id, model.id)] = (provider, model)
        provider_rows.append({
            "provider": provider.spec.id,
            "cataloged_models": len(provider.spec.models),
            "excluded_by_reason": excluded,
            "excluded_models": sum(excluded.values()),
        })

    decisions = rank(providers, req)
    preview = []
    for decision in decisions[:limit]:
        provider, model = by_key[(decision.provider, decision.model)]
        state = provider.state.models.get(model.id)
        samples = state.success_count + state.failure_count if state else 0
        preview.append({
            "provider": decision.provider,
            "model": decision.model,
            "score": round(decision.score, 3),
            "reasons": decision.reasons,
            "required_capability_evidence": {
                cap: _dimension(model, ("code", "coding") if cap in {"code", "coding"} else (cap,))["status"]
                for cap in sorted(req.required_capabilities)
            },
            "observed_attempts": samples,
            "observed_success_rate": (
                round(state.success_count / samples, 4) if samples else None
            ),
            "observed_latency_ms": state.ewma_latency_ms if samples else None,
            "quality_score": None,
        })
    return {
        "scope": "configured_providers_local_snapshot_only",
        "live_probes_performed": False,
        "inference_performed": False,
        "account_billing_verified": False,
        "quality_measured": False,
        "task_interpretation": analysis,
        "cataloged_models": sum(row["cataloged_models"] for row in provider_rows),
        "ranked_models": len(decisions),
        "preview_limit": limit,
        "preview_truncated": len(decisions) > limit,
        "providers": provider_rows,
        "ranking": preview,
    }
