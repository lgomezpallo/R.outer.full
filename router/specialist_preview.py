"""Read-only specialist model ranking and typed fixture simulation.

This is NOT an image/audio execution endpoint. All provider eligibility checks
are inherited from the ordinary selector, including account billing gates.
The only artifact-producing operation requires the exact fixture-only adapter.
No credentials, prompts, or binary payloads are included in the preview.
"""
from __future__ import annotations

from .registry import RegisteredProvider
from .selector import ineligibility_reason, model_coverage, rank
from .specialist_contract import (
    SpecialistRequest,
    SpecialistResult,
    specialist_execution_plan,
)
from .specialist_offline import OfflineFixtureTransport
from .types import RouteRequest


def preview_specialist_ranking(
    providers: list[RegisteredProvider],
    request: SpecialistRequest,
    *,
    max_strategic_cost: int | None = None,
) -> dict:
    """Rank only against hard modality and billing constraints, without I/O.

    A ranked model is a hypothetical selector result, NOT permission to invoke
    it: specialist transports remain unimplemented and account verification is
    independent of model pricing. The result intentionally omits task content,
    credentials and artifact bytes.
    """
    if not isinstance(request, SpecialistRequest):
        raise ValueError("invalid_specialist_preview_request")
    if max_strategic_cost is not None and (
        type(max_strategic_cost) is not int or max_strategic_cost < 0
    ):
        raise ValueError("invalid_specialist_strategic_cost")

    route_request = RouteRequest(
        task=request.prompt or "Transcribe an audio artifact",
        required_capabilities=frozenset({request.kind}),
        timeout_s=request.timeout_s,
        decompose=False,
        max_strategic_cost=max_strategic_cost,
        application_name="specialist-offline-preview",
    )
    ranked = rank(providers, route_request)
    coverage = model_coverage(providers, route_request)
    exclusions = [
        {"provider": provider.spec.id, "model": model.id, "reason": reason}
        for provider in providers
        for model in provider.spec.models
        if (reason := ineligibility_reason(provider, model, route_request)) is not None
    ]
    return {
        **specialist_execution_plan(request),
        "mode": "read_only_offline_preview",
        "ranked_models": [
            {
                "provider": decision.provider,
                "model": decision.model,
                "score": decision.score,
                "reasons": list(decision.reasons),
            }
            for decision in ranked
        ],
        "excluded_models": exclusions,
        "coverage": coverage,
        "fixture_simulation_available": True,
        # Never override the contract's execution_allowed=False.
    }


def simulate_ranked_specialist_fixture(
    providers: list[RegisteredProvider],
    request: SpecialistRequest,
    transport: OfflineFixtureTransport,
    *,
    max_strategic_cost: int | None = None,
) -> tuple[dict, SpecialistResult]:
    """Test the typed artifact contract; do not execute a selected model.

    Simulation is independent of model eligibility: an empty ranking must
    never be mistaken for a successful provider invocation. No arbitrary
    SpecialistTransport or injectable network client is accepted.
    """
    if type(transport) is not OfflineFixtureTransport:
        raise ValueError("fixture_only_specialist_transport_required")
    preview = preview_specialist_ranking(
        providers, request, max_strategic_cost=max_strategic_cost
    )
    result = transport.run_offline(request)
    return preview, result
