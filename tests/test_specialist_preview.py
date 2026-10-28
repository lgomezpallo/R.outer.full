"""Synthetic specialist ranking + fixture contract integration (zero API calls)."""
import socket

import pytest

from router.catalog import CapabilityEvidence, ModelSpec, ProviderSpec
from router.registry import RegisteredProvider
from router.specialist_contract import SpecialistRequest, SpecialistResult
from router.specialist_offline import OfflineFixtureTransport
from router.specialist_preview import (
    preview_specialist_ranking,
    simulate_ranked_specialist_fixture,
)
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
        api_key="not-a-real-key",
        state=RuntimeState(),
    )


def _model(name, kind="image_generation", *, status="free", evidence=(), cost=5):
    return ModelSpec(
        id=name,
        capabilities=frozenset({kind}),
        model_class="specialist",
        billing_status=status,
        evidence=tuple(evidence),
        strategic_cost=cost,
    )


def test_default_account_billing_gate_blocks_even_free_specialists():
    req = SpecialistRequest("image_generation", "Create a tree")
    p = _provider(_model("flux-free-labelled"), _model("flux-paid", status="paid"))
    preview = preview_specialist_ranking([p], req)
    assert preview["ranked_models"] == []
    assert preview["execution_allowed"] is False
    assert preview["account_billing_verified"] is False
    assert preview["transport"] == "not_implemented"
    reasons = {row["model"]: row["reason"] for row in preview["excluded_models"]}
    assert reasons == {
        "flux-free-labelled": "billing_unverified_account",
        "flux-paid": "billing_paid",
    }
    assert preview["coverage"]["totals"]["eligible_models"] == 0
    assert "Create a tree" not in repr(preview)
    assert "not-a-real-key" not in repr(preview)


def test_verified_capability_ranks_before_declared_when_account_gate_is_mocked(monkeypatch):
    # This monkeypatch is TEST-ONLY; production account verification stays blocked.
    monkeypatch.setattr("router.billing.account_billing_ineligibility_reason", lambda _: None)
    evidence = (CapabilityEvidence("image_generation", "verified", "synthetic", "2026-10-28"),)
    unsupported = (CapabilityEvidence("image_generation", "unsupported", "synthetic", "2026-10-28"),)
    p = _provider(
        _model("flux-declared"),
        _model("flux-verified", evidence=evidence),
        _model("flux-unsupported", evidence=unsupported),
        _model("flux-paid", status="paid"),
        _model("generic-chat", kind="chat"),
    )
    preview = preview_specialist_ranking([p], SpecialistRequest("image_generation", "A tree"))
    assert [item["model"] for item in preview["ranked_models"]] == [
        "flux-verified", "flux-declared"
    ]
    reasons = {row["model"]: row["reason"] for row in preview["excluded_models"]}
    assert reasons["flux-unsupported"] == "unsupported_capability"
    assert reasons["flux-paid"] == "billing_paid"
    assert reasons["generic-chat"] == "missing_capability"
    assert preview["execution_allowed"] is False
    assert preview["account_billing_verified"] is False


def test_strategic_cost_limit_applies_even_in_preview(monkeypatch):
    monkeypatch.setattr("router.billing.account_billing_ineligibility_reason", lambda _: None)
    p = _provider(_model("flux-cheap", cost=2), _model("flux-expensive", cost=90))
    preview = preview_specialist_ranking(
        [p], SpecialistRequest("image_generation", "A tree"), max_strategic_cost=20
    )
    assert [row["model"] for row in preview["ranked_models"]] == ["flux-cheap"]
    assert any(row["reason"] == "strategic_cost" for row in preview["excluded_models"])
    assert preview["execution_allowed"] is False


@pytest.mark.parametrize("kind,job,result", [
    ("image_generation", SpecialistRequest("image_generation", "A tree"),
     SpecialistResult("image_generation", b"fixture-image", "image/png")),
    ("image_editing", SpecialistRequest("image_editing", "Crop", b"source-image", "image/png"),
     SpecialistResult("image_editing", b"fixture-edited", "image/webp")),
    ("speech", SpecialistRequest("speech", "Hello"),
     SpecialistResult("speech", b"fixture-audio", "audio/wav")),
    ("transcription", SpecialistRequest("transcription", input_bytes=b"source-audio", input_media_type="audio/wav"),
     SpecialistResult("transcription", text="fixture transcript")),
])
def test_all_modalities_fixture_simulate_without_network(monkeypatch, kind, job, result):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in offline tests")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    preview, actual = simulate_ranked_specialist_fixture(
        [_provider(_model("flux-model"))], job, OfflineFixtureTransport({kind: result})
    )
    assert actual is result
    assert preview["kind"] == kind
    assert preview["required_capability"] == kind
    assert preview["execution_allowed"] is False
    assert preview["ranked_models"] == []
    assert preview["fixture_simulation_available"] is True
    assert "source-image" not in repr(preview)
    assert "source-audio" not in repr(preview)
    assert "fixture-image" not in repr(preview)


def test_rejects_arbitrary_or_subclassed_transport():
    request = SpecialistRequest("speech", "Hello")
    result = SpecialistResult("speech", b"fixture", "audio/wav")

    class PotentialNetworkAdapter(OfflineFixtureTransport):
        pass

    for adapter in (object(), PotentialNetworkAdapter({"speech": result})):
        with pytest.raises(ValueError, match="fixture_only_specialist_transport_required"):
            simulate_ranked_specialist_fixture([], request, adapter)


def test_missing_fixture_fails_closed_without_provider_attempt():
    with pytest.raises(LookupError, match="offline_specialist_fixture_missing"):
        simulate_ranked_specialist_fixture(
            [_provider(_model("flux"))],
            SpecialistRequest("image_generation", "A tree"),
            OfflineFixtureTransport({}),
        )


@pytest.mark.parametrize("value", [-1, 1.2, True, "10"])
def test_rejects_invalid_strategic_cost(value):
    with pytest.raises(ValueError, match="invalid_specialist_strategic_cost"):
        preview_specialist_ranking(
            [], SpecialistRequest("speech", "Hello"), max_strategic_cost=value
        )


def test_rejects_non_specialist_request():
    with pytest.raises(ValueError, match="invalid_specialist_preview_request"):
        preview_specialist_ranking([], object())
