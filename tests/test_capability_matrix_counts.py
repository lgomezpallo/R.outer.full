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
    assert report["inventory_unit"] == "local_catalog_entries_not_live_models"
    assert report["last_snapshot_flag_is_not_live_freshness"] is True
    inventory = report["inventory_counts"]
    assert inventory["catalog_entries"] == 130
    assert inventory["distinct_model_ids"] == 130
    assert inventory["duplicate_model_entries"] == 0
    assert inventory["reported_free_price_entries"] == 2
    assert inventory["reported_paid_price_entries"] == 1
    assert inventory["unverified_price_entries"] == 127
    assert inventory["reported_free_price_not_in_last_snapshot_entries"] == 2


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


def test_inventory_provenance_counts_duplicate_and_stale_free_entries_offline():
    models = [
        ModelSpec("vendor/duplicate", frozenset({"chat"}), "unknown",
                  billing_status="free", discovered_in_last_snapshot=True),
        ModelSpec("vendor/duplicate", frozenset({"chat"}), "unknown",
                  billing_status="paid", discovered_in_last_snapshot=False),
        ModelSpec("vendor/stale-free", frozenset({"chat"}), "unknown",
                  billing_status="free", discovered_in_last_snapshot=False),
        ModelSpec("vendor/unknown", frozenset({"chat"}), "unknown",
                  billing_status="unknown", discovered_in_last_snapshot=True),
    ]
    report = capability_report([_groq(models)])
    counts = report["inventory_counts"]
    assert counts == report["providers"][0]["inventory_counts"]
    assert counts == {
        "catalog_entries": 4,
        "distinct_model_ids": 3,
        "duplicate_model_entries": 1,
        "reported_free_price_entries": 2,
        "reported_paid_price_entries": 1,
        "unverified_price_entries": 1,
        "observed_in_last_snapshot_entries": 2,
        "reported_free_price_not_in_last_snapshot_entries": 1,
    }
    assert report["account_billing_verified"] is False
    assert report["reported_free_price_is_not_no_charge_guarantee"] is True
    assert report["providers"][0]["last_inventory_merge_at"] is None
    assert report["providers"][0]["last_inventory_source"] is None
    assert all(
        row["inventory_counts"]["catalog_entries"] == 0
        for row in report["providers"][1:]
    )


def test_unconfigured_inventory_counters_are_zero():
    report = capability_report([])
    assert all(value == 0 for value in report["inventory_counts"].values())
    assert all(
        all(value == 0 for value in row["inventory_counts"].values())
        for row in report["providers"]
    )
