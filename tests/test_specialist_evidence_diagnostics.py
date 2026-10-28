"""Offline specialist evidence diagnostics: local records are not account approval."""
import socket

import pytest

from router.catalog import CapabilityEvidence, ModelSpec, ProviderSpec
from router.registry import RegisteredProvider
from router.specialist_contract import SpecialistRequest
from router.specialist_preview import preview_specialist_ranking
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing


def _provider(*models):
    return RegisteredProvider(
        spec=ProviderSpec(
            id="groq",
            aliases=("groq",),
            base_url="https://api.groq.com/openai/v1",
            models=tuple(models),
            strategic_cost=10,
        ),
        api_key="SECRET_API_KEY_SHOULD_NOT_LEAK",
        state=RuntimeState(),
    )


def _model(name, *, evidence=(), billing="free", observed=False):
    return ModelSpec(
        id=name,
        capabilities=frozenset({"image_generation"}),
        model_class="specialist",
        evidence=tuple(evidence),
        billing_status=billing,
        discovered_in_last_snapshot=observed,
        strategic_cost=5,
    )


def test_diagnostics_scale_and_separate_provenance_from_account_gate(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("offline diagnostics must not open a socket")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    verified = CapabilityEvidence(
        "image_generation", "verified", "SECRET_EVIDENCE_SOURCE_SHOULD_NOT_LEAK", "2026-10-28"
    )
    unsupported = CapabilityEvidence("image_generation", "unsupported", "synthetic", "2026-10-28")
    legacy = CapabilityEvidence("image_generation", "verified", "migrated_from_v1", "2026-10-28")
    models = [_model(f"synthetic-{i}") for i in range(130)]
    models.extend([
        _model("synthetic-verified", evidence=(verified,), observed=True),
        _model("synthetic-conflicting", evidence=(verified, unsupported)),
        _model("synthetic-legacy", evidence=(legacy,), billing="unknown"),
    ])
    preview = preview_specialist_ranking(
        [_provider(*models)],
        SpecialistRequest("image_generation", "SECRET_PROMPT_SHOULD_NOT_LEAK"),
    )
    diag = preview["evidence_diagnostics"]
    assert diag["model_entries"] == 133
    assert diag["evidence_status_counts"] == {
        "conflicting_records": 1,
        "declared_only": 130,
        "legacy_or_incomplete_evidence": 1,
        "recorded_verified": 1,
    }
    assert diag["selection_gate_counts"] == {
        "billing_unverified_account": 131,
        "billing_unverified": 1,
        "unsupported_capability": 1,
    }
    rows = {item["model"]: item for item in diag["models"]}
    assert rows["synthetic-verified"]["capability_evidence_status"] == "recorded_verified"
    assert rows["synthetic-verified"]["observed_in_last_snapshot"] is True
    assert rows["synthetic-conflicting"]["selection_gate"] == "unsupported_capability"
    assert rows["synthetic-legacy"]["reported_price"] == "unknown"
    assert diag["account_billing_verified"] is False
    assert diag["quality_measured"] is False
    assert diag["recorded_evidence_independently_verified"] is False
    assert diag["reported_free_price_is_not_account_approval"] is True
    assert preview["execution_allowed"] is False
    assert preview["ranked_models"] == []
    for secret in ("SECRET_PROMPT", "SECRET_EVIDENCE", "SECRET_API_KEY"):
        assert secret not in repr(preview)


def test_diagnostics_track_ranked_and_paid_rows_without_granting_execution(monkeypatch):
    # Mocked account check only for testing ranking behavior, never production approval.
    monkeypatch.setattr("router.billing.account_billing_ineligibility_reason", lambda _: None)
    provider = _provider(
        _model("candidate", observed=True),
        _model("paid-candidate", billing="paid"),
    )
    preview = preview_specialist_ranking(
        [provider], SpecialistRequest("image_generation", "A tree")
    )
    rows = {item["model"]: item for item in preview["evidence_diagnostics"]["models"]}
    assert rows["candidate"]["rankable_offline"] is True
    assert rows["candidate"]["selection_gate"] is None
    assert rows["paid-candidate"]["rankable_offline"] is False
    assert rows["paid-candidate"]["selection_gate"] == "billing_paid"
    assert preview["evidence_diagnostics"]["selection_gate_counts"] == {"billing_paid": 1}
    assert [row["model"] for row in preview["ranked_models"]] == ["candidate"]
    assert preview["execution_allowed"] is False
    assert preview["account_billing_verified"] is False


def test_duplicate_model_entries_are_visible_and_blocked(monkeypatch):
    monkeypatch.setattr("router.billing.account_billing_ineligibility_reason", lambda _: None)
    provider = _provider(_model("duplicate"), _model("duplicate"))
    preview = preview_specialist_ranking(
        [provider], SpecialistRequest("image_generation", "A tree")
    )
    diag = preview["evidence_diagnostics"]
    assert diag["model_entries"] == 2
    assert diag["selection_gate_counts"] == {"billing_ambiguous_model": 2}
    assert all(row["rankable_offline"] is False for row in diag["models"])
    assert preview["ranked_models"] == []
