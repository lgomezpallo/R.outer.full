"""Stage 3 offline capability matrix over large inventories; no provider traffic."""
from dataclasses import replace

import pytest

from router.capability_evidence import DIMENSIONS, EVIDENCE_STATUSES, capability_report
from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing


def _groq(models):
    spec = next(provider for provider in BUILTINS if provider.id == "groq")
    return RegisteredProvider(
        replace(spec, models=tuple(models)), "offline-only", RuntimeState()
    )


def _record(capability, status, source="offline-fixture"):
    return CapabilityEvidence(capability, status, source, "2026-10-27T19:00:00Z")


def test_130_model_inventory_matrix_counts_every_dimension_without_claiming_quality():
    models = [
        ModelSpec(f"vendor/basic-{i}", frozenset({"chat"}), "unknown")
        for i in range(130)
    ]
    models[0] = replace(models[0], billing_status="free",
                        evidence=(_record("code", "verified"),))
    models[1] = replace(models[1], billing_status="free",
                        evidence=(_record("code", "unsupported"),))
    models[2] = replace(models[2], billing_status="paid",
                        evidence=(_record("code", "inconclusive"),))
    report = capability_report([_groq(models)])
    assert report["live_probes_performed"] is False
    assert report["account_billing_verified"] is False
    assert report["reported_free_price_is_not_no_charge_guarantee"] is True
    assert report["summary_unit"] == "catalog_model_dimension_pairs"
    counts = report["dimension_status_counts"]
    assert counts["code"]["recorded_verified"] == 1
    assert counts["code"]["recorded_unsupported"] == 1
    assert counts["code"]["recorded_inconclusive"] == 1
    assert counts["code"]["unknown"] == 127
    for dimension in DIMENSIONS:
        assert sum(counts[dimension].values()) == 130
    free = report["reported_free_price_dimension_status_counts"]["code"]
    assert free["recorded_verified"] == 1
    assert free["recorded_unsupported"] == 1
    assert free["recorded_inconclusive"] == 0
    assert sum(free.values()) == 2
    assert report["providers"][0]["dimension_status_counts"] == counts


def test_conflicting_alias_and_legacy_records_do_not_count_as_verified():
    models = [
        ModelSpec("vendor/code-one", frozenset({"code"}), "unknown",
                  evidence=(_record("code", "verified"), _record("coding", "unsupported")),
                  billing_status="free"),
        ModelSpec("vendor/basic-two", frozenset({"reasoning"}), "unknown",
                  evidence=(_record("reasoning", "verified", "migrated_from_v1"),),
                  billing_status="free"),
    ]
    report = capability_report([_groq(models)])
    assert report["dimension_status_counts"]["code"]["conflicting_records"] == 1
    assert report["dimension_status_counts"]["code"]["recorded_verified"] == 0
    assert report["dimension_status_counts"]["reasoning"]["legacy_or_incomplete_evidence"] == 1
    assert report["reported_free_price_dimension_status_counts"]["code"]["recorded_verified"] == 0


def test_unconfigured_providers_have_zero_matrix_and_no_implicit_free_models():
    report = capability_report([])
    assert len(report["providers"]) == 4
    assert all(not row["configured"] for row in report["providers"])
    for matrix in (
        report["dimension_status_counts"],
        report["reported_free_price_dimension_status_counts"],
    ):
        assert set(matrix) == set(DIMENSIONS)
        assert all(set(statuses) == set(EVIDENCE_STATUSES) for statuses in matrix.values())
        assert all(sum(statuses.values()) == 0 for statuses in matrix.values())
