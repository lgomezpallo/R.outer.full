"""Offline per-model capability evidence, distinct from model-name guesses.

This report never probes a provider, changes routing eligibility, or treats
advertised capabilities as verified. A saved probe record is not independently
authenticated and never constitutes account billing approval.
"""
from __future__ import annotations

from .catalog import ModelSpec, infer_discovered_capabilities
from .inventory import SUPPORTED_PROVIDERS
from .registry import RegisteredProvider

# User-facing dimensions and their existing catalog capability identifiers.
# Audio input/output are separate: speech synthesis is not transcription.
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "chat": ("chat",),
    "code": ("code", "coding"),
    "reasoning": ("reasoning",),
    "spanish": ("spanish",),
    "vision": ("vision",),
    "documents": ("document",),
    "context": ("long_context",),
    "json": ("json",),
    "audio_transcription": ("transcription",),
    "audio_speech": ("speech",),
    "tools": ("tools",),
}

LEGACY_MARKER = "migrated_from_v1"


def _dimension(model: ModelSpec, capabilities: tuple[str, ...]) -> dict:
    names = set(capabilities)
    declared = sorted(names & model.capabilities)
    inferred = sorted(names & infer_discovered_capabilities(model.id))
    records = [item for item in model.evidence if item.capability in names]
    # Old 'verified_capabilities' imports were not checked: never promote them.
    current = [
        item for item in records
        if item.evidence.strip() and item.evidence != LEGACY_MARKER
        and item.checked_at.strip()
    ]
    statuses = {item.status for item in current}
    if {"verified", "unsupported"} <= statuses:
        status = "conflicting_records"
    elif "unsupported" in statuses:
        status = "recorded_unsupported"
    elif "verified" in statuses:
        status = "recorded_verified"
    elif "inconclusive" in statuses:
        status = "recorded_inconclusive"
    elif records:
        status = "legacy_or_incomplete_evidence"
    elif declared:
        status = "declared_only"
    elif inferred:
        status = "inferred_from_name_only"
    else:
        status = "unknown"

    return {
        "status": status,
        "declared_capabilities": declared,
        "name_inferred_capabilities": inferred,
        "evidence": [
            {
                "capability": item.capability,
                "status": item.status,
                "source": item.evidence,
                "checked_at": item.checked_at,
                "legacy_unverified": item.evidence == LEGACY_MARKER,
            }
            for item in records
        ],
    }


def capability_report(providers: list[RegisteredProvider]) -> dict:
    """Read existing catalog snapshots only; zero HTTP and zero inference."""
    registered = {provider.spec.id: provider for provider in providers}
    rows = []
    for provider_id in SUPPORTED_PROVIDERS:
        provider = registered.get(provider_id)
        models = []
        if provider:
            for model in provider.spec.models:
                models.append({
                    "id": model.id,
                    "dimensions": {
                        dimension: _dimension(model, capabilities)
                        for dimension, capabilities in DIMENSIONS.items()
                    },
                })
        rows.append({
            "provider": provider_id,
            "configured": provider is not None,
            "models": models,
        })
    return {
        "scope": "configured_providers_local_catalog_only",
        "live_probes_performed": False,
        "recorded_evidence_independently_verified": False,
        "declared_or_name_inferred_is_not_verified": True,
        "account_billing_verified": False,
        "dimensions": list(DIMENSIONS),
        "providers": rows,
    }
