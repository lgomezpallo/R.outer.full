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
    "image_generation": ("image_generation",),
    "image_editing": ("image_editing",),
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


# A summary of catalog model/dimension pairs is useful when a provider
# advertises hundreds of models. These are *evidence labels*, not a benchmark.
EVIDENCE_STATUSES = (
    "recorded_verified", "recorded_unsupported", "conflicting_records",
    "recorded_inconclusive", "legacy_or_incomplete_evidence",
    "declared_only", "inferred_from_name_only", "unknown",
)


def _empty_matrix() -> dict[str, dict[str, int]]:
    return {
        dimension: {status: 0 for status in EVIDENCE_STATUSES}
        for dimension in DIMENSIONS
    }


def _accumulate(matrix: dict, dimensions: dict) -> None:
    for dimension, result in dimensions.items():
        matrix[dimension][result["status"]] += 1


# Catalog inventory counters describe local entries, not live availability.
# Duplicate IDs and stale free-price labels must remain visible to operators.
INVENTORY_FIELDS = (
    "catalog_entries",
    "distinct_model_ids",
    "duplicate_model_entries",
    "reported_free_price_entries",
    "reported_paid_price_entries",
    "unverified_price_entries",
    "observed_in_last_snapshot_entries",
    "reported_free_price_not_in_last_snapshot_entries",
)


def _inventory_counts(models: tuple[ModelSpec, ...]) -> dict[str, int]:
    ids = {model.id for model in models}
    return {
        "catalog_entries": len(models),
        "distinct_model_ids": len(ids),
        "duplicate_model_entries": len(models) - len(ids),
        "reported_free_price_entries": sum(m.billing_status == "free" for m in models),
        "reported_paid_price_entries": sum(m.billing_status == "paid" for m in models),
        "unverified_price_entries": sum(m.billing_status not in {"free", "paid"} for m in models),
        "observed_in_last_snapshot_entries": sum(m.discovered_in_last_snapshot for m in models),
        "reported_free_price_not_in_last_snapshot_entries": sum(
            m.billing_status == "free" and not m.discovered_in_last_snapshot for m in models
        ),
    }


def capability_report(providers: list[RegisteredProvider]) -> dict:
    """Read local catalog only; count evidence, not measured model quality.

    Matrix cells count catalog model/dimension pairs. The free-price subset is
    provider-reported, NOT a claim of free account usage or routing approval.
    """
    registered = {provider.spec.id: provider for provider in providers}
    totals = _empty_matrix()
    free_totals = _empty_matrix()
    inventory_totals = {field: 0 for field in INVENTORY_FIELDS}
    rows = []
    for provider_id in SUPPORTED_PROVIDERS:
        provider = registered.get(provider_id)
        models = []
        counts = _empty_matrix()
        free_counts = _empty_matrix()
        inventory = _inventory_counts(provider.spec.models if provider else ())
        for field in INVENTORY_FIELDS:
            inventory_totals[field] += inventory[field]
        if provider:
            for model in provider.spec.models:
                dimensions = {
                    dimension: _dimension(model, capabilities)
                    for dimension, capabilities in DIMENSIONS.items()
                }
                models.append({"id": model.id, "dimensions": dimensions})
                _accumulate(counts, dimensions)
                _accumulate(totals, dimensions)
                if model.billing_status == "free":
                    _accumulate(free_counts, dimensions)
                    _accumulate(free_totals, dimensions)
        rows.append({
            "provider": provider_id,
            "configured": provider is not None,
            "models": models,
            "inventory_counts": inventory,
            "last_inventory_merge_at": provider.spec.last_inventory_merge_at if provider else None,
            "last_inventory_source": provider.spec.last_inventory_source if provider else None,
            "dimension_status_counts": counts,
            "reported_free_price_dimension_status_counts": free_counts,
        })
    return {
        "scope": "configured_providers_local_catalog_only",
        "live_probes_performed": False,
        "recorded_evidence_independently_verified": False,
        "declared_or_name_inferred_is_not_verified": True,
        "account_billing_verified": False,
        "summary_unit": "catalog_model_dimension_pairs",
        "inventory_unit": "local_catalog_entries_not_live_models",
        "last_snapshot_flag_is_not_live_freshness": True,
        "inventory_counts": inventory_totals,
        "reported_free_price_is_not_no_charge_guarantee": True,
        "dimensions": list(DIMENSIONS),
        "dimension_status_counts": totals,
        "reported_free_price_dimension_status_counts": free_totals,
        "providers": rows,
    }
