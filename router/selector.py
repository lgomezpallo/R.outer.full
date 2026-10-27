from __future__ import annotations
from . import billing
from .billing import CLOUDFLARE_PAID_ONLY, NVIDIA_ROUTING_ALLOWLIST, billing_endpoint_ineligibility_reason
from .catalog import infer_discovered_capabilities
from .registry import RegisteredProvider
from .ranking_evidence import verification_tier
from .task_requirements import analyze_task
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
    return verification_tier(model, required)

def _specialization_penalty(model, required: frozenset[str]) -> int:
    extras = (set(model.capabilities) | set(infer_discovered_capabilities(model.id))) - set(required) - set(GENERIC_EXTRAS)
    return sum(1 for item in extras if item in SPECIALIZED_CAPABILITIES)

def billing_ineligibility_reason(provider: RegisteredProvider, model) -> str | None:
    """Pure billing gate shared by routing, probes, and provider audits.

    Cloudflare Workers AI's per-model inventory does not attest account plan
    or remaining free neurons. Until an account-level no-charge guarantee can
    be verified, Cloudflare models must not generate inference requests.
    """
    # A manually edited or legacy persisted catalog may contain duplicate
    # model IDs with contradictory prices. Discovery deduplicates its own
    # responses, but do not trust the persisted catalog to be canonical.
    # Even two identical rows are ambiguous and must not enter the ranking.
    if sum(candidate.id == model.id for candidate in provider.spec.models) > 1:
        return "billing_ambiguous_model"
    # Check destination and protocol before accepting a catalog model.
    # This also rejects unknown providers and forged provider ids.
    endpoint_block = billing_endpoint_ineligibility_reason(
        provider.spec.id, provider.spec.base_url, provider.spec.protocol
    )
    if endpoint_block is not None:
        return endpoint_block
    if model.billing_status == "paid":
        return "billing_paid"
    if provider.spec.id in {"groq", "nvidia", "openrouter"}:
        # Historical Groq/NVIDIA routing lists and model names are not proof
        # of zero price. All three providers require explicit per-model free
        # evidence, independently of account-level no-overage verification.
        if model.billing_status != "free":
            return "billing_paid" if model.billing_status == "paid" else "billing_unverified"
    if provider.spec.id == "nvidia" and model.id not in NVIDIA_ROUTING_ALLOWLIST:
        return "billing_unverified"
    if provider.spec.id == "cloudflare":
        if model.id in CLOUDFLARE_PAID_ONLY or model.billing_status == "paid":
            return "billing_paid"
        # A free allocation can become billable on Workers Paid accounts.
        return "billing_unverified"
    return billing.account_billing_ineligibility_reason(provider.spec.id)


def ineligibility_reason(provider: RegisteredProvider, model, req: RouteRequest) -> str | None:
    """Explain why a cataloged provider/model cannot enter this request's ranking.

    This is the same gate used by _score, so coverage counts never pretend that
    an unavailable or incompatible catalog entry is actually selectable.
    """
    model_name = model.id.casefold()
    if "chat" in req.required_capabilities and not infer_discovered_capabilities(model.id) and "chat" not in model.verified_capabilities:
        return "not_chat_model"
    if "diffusiongemma" in model_name and "chat" in req.required_capabilities:
        return "not_chat_model"
    if "allam-" in model_name and "chat" in req.required_capabilities:
        task_text = req.task + " " + req.context
        if not any("\u0600" <= char <= "\u06ff" for char in task_text):
            return "language_mismatch"
    if req.required_capabilities & model.unsupported_capabilities:
        return "unsupported_capability"
    # A recorded unsupported capability also blocks its alias (code/coding).
    # Conflicting verified/unsupported records never grant eligibility.
    if verification_tier(model, req.required_capabilities) == 9:
        return "unsupported_capability"
    effective_capabilities = model.capabilities | model.verified_capabilities
    if req.required_capabilities - effective_capabilities:
        return "missing_capability"
    billing_reason = billing_ineligibility_reason(provider, model)
    if billing_reason is not None:
        return billing_reason
    if not provider.state.available:
        return "provider_cooldown"
    model_state = provider.state.models.get(model.id)
    if model_state is not None and not model_state.available:
        return "model_cooldown"
    strategic_cost = provider.spec.strategic_cost + model.strategic_cost
    if req.max_strategic_cost is not None and strategic_cost > req.max_strategic_cost:
        return "strategic_cost"
    return None


def model_coverage(providers: list[RegisteredProvider], req: RouteRequest) -> dict:
    """Read-only accounting of catalog entries versus actually rankable models.

    A catalog entry is not proof that a provider accepts requests or that its
    answer quality has been measured. Counts are for configured providers only.
    """
    rows = []
    totals = {
        "cataloged_models": 0,
        "eligible_models": 0,
        "excluded_models": 0,
        "observed_models": 0,
        "observed_attempts": 0,
        "verified_eligible_models": 0,
    }
    for provider in providers:
        row = {
            "provider": provider.spec.id,
            "cataloged_models": len(provider.spec.models),
            "eligible_models": 0,
            "excluded_models": 0,
            "excluded_by_reason": {},
            "observed_models": 0,
            "observed_attempts": 0,
            "verified_eligible_models": 0,
        }
        for model in provider.spec.models:
            state = provider.state.models.get(model.id)
            samples = (state.success_count + state.failure_count) if state else 0
            if samples:
                row["observed_models"] += 1
                row["observed_attempts"] += samples
            reason = ineligibility_reason(provider, model, req)
            if reason is not None:
                row["excluded_models"] += 1
                row["excluded_by_reason"][reason] = row["excluded_by_reason"].get(reason, 0) + 1
            else:
                row["eligible_models"] += 1
                # Use the same evidence policy as the ranking: migrated legacy
                # records, incomplete evidence and contradictions are NOT verified.
                if verification_tier(model, req.required_capabilities) == 0:
                    row["verified_eligible_models"] += 1
        rows.append(row)
        for key in totals:
            totals[key] += row[key]
    return {
        "required_capabilities": sorted(req.required_capabilities),
        "max_strategic_cost": req.max_strategic_cost,
        "catalog_scope": "configured_providers_only",
        "selection_scope": "current_request_and_runtime_cooldowns",
        "quality_measured": False,
        "totals": totals,
        "providers": rows,
    }


def _score(
    provider: RegisteredProvider,
    model,
    req: RouteRequest,
    scarcity: dict[str, int] | None = None,
    advisory: frozenset[str] = frozenset(),
) -> Decision | None:
    if ineligibility_reason(provider, model, req) is not None:
        return None

    strategic_cost = provider.spec.strategic_cost + model.strategic_cost
    health_rank = _provider_health_rank(provider)
    verification_tier = _verification_tier(model, req.required_capabilities)
    # Task hints affect preference, never eligibility or required capabilities.
    # In particular, an unverified hint cannot authorize a model or account.
    scoring_needs = req.required_capabilities | advisory
    specialization_penalty = _specialization_penalty(model, scoring_needs)
    # Preserve specialized capabilities only when the current task does not need them.
    # Smaller pools incur larger penalties; declared availability is provisional.
    scarce_extras = (set(model.capabilities) | set(model.verified_capabilities) | set(infer_discovered_capabilities(model.id))) - set(scoring_needs) - set(GENERIC_EXTRAS)
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
    # Prefer a suitable specialist for a clear task, without making a hint
    # mandatory. Recorded unsupported/conflicting evidence never earns a bonus.
    advisory_bonus = 0.0
    for capability in sorted(advisory):
        if capability in model.unsupported_capabilities:
            continue
        if capability not in model.capabilities and capability not in model.verified_capabilities:
            continue
        tier = _verification_tier(model, frozenset({capability}))
        advisory_bonus += {0: 500.0, 1: 120.0}.get(tier, 0.0)
    score += advisory_bonus
    reasons.append(f"advisory_task_bonus={advisory_bonus:.0f}")
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
    # Interpret the task once per request. Suggestions influence only ordering;
    # ineligibility_reason remains the sole capability/billing gate.
    analysis = analyze_task(req.task)
    advisory = frozenset(
        item["capability"] for item in analysis["suggested_capabilities"]
        if item["capability"] in SPECIALIZED_CAPABILITIES
    ) - req.required_capabilities
    decisions: list[Decision] = []
    scarcity: dict[str, int] = {}
    for provider in providers:
        for model in provider.spec.models:
            # Scarcity must describe the *rankable* pool for this request.
            # Paid, unverified, incompatible or cooling models cannot supply
            # a specialized capability and must not dilute its scarcity cost.
            # Reuse the exact gate that _score applies below.
            if ineligibility_reason(provider, model, req) is not None:
                continue
            for capability in (model.capabilities | model.verified_capabilities | infer_discovered_capabilities(model.id)) - model.unsupported_capabilities:
                scarcity[capability] = scarcity.get(capability, 0) + 1
    for provider in providers:
        for model in provider.spec.models:
            decision = _score(provider, model, req, scarcity, advisory)
            if decision:
                decisions.append(decision)
    return sorted(decisions, key=lambda d: d.score, reverse=True)
