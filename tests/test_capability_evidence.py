"""Capability evidence reporting tests: exclusively local fixtures, no inference."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from router.capability_evidence import DIMENSIONS, capability_report
from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing


def _provider(models):
    spec = next(item for item in BUILTINS if item.id == "groq")
    return RegisteredProvider(
        replace(spec, models=tuple(models)), "offline-placeholder", RuntimeState()
    )


def _model(model_id="vendor/basic", declared=(), evidence=()):
    return ModelSpec(
        model_id, frozenset(declared), "unknown",
        evidence=tuple(evidence), billing_status="unknown",
    )


def _record(capability, status, source="offline-fixture", checked="2026-10-26T13:00:00Z"):
    return CapabilityEvidence(capability, status, source, checked)


def _entry(model):
    report = capability_report([_provider([model])])
    assert report["live_probes_performed"] is False
    assert report["account_billing_verified"] is False
    return report["providers"][0]["models"][0]["dimensions"]


def test_all_required_dimensions_and_audio_modalities_are_distinct():
    assert set(DIMENSIONS) >= {
        "code", "reasoning", "spanish", "vision", "documents", "context",
        "json", "audio_transcription", "audio_speech", "tools",
        "image_generation", "image_editing",
    }
    dimensions = _entry(_model(
        "vendor/transcriber", declared=("transcription",),
        evidence=(_record("transcription", "verified"),),
    ))
    assert dimensions["audio_transcription"]["status"] == "recorded_verified"
    assert dimensions["audio_speech"]["status"] == "unknown"


def test_image_generation_and_editing_evidence_are_independent():
    dimensions = _entry(_model(
        "vendor/image-specialist",
        declared=("image_generation", "image_editing"),
        evidence=(
            _record("image_generation", "verified"),
            _record("image_editing", "unsupported"),
        ),
    ))
    assert dimensions["image_generation"]["status"] == "recorded_verified"
    assert dimensions["image_editing"]["status"] == "recorded_unsupported"
    assert dimensions["vision"]["status"] == "unknown"


def test_declared_or_inferred_from_name_is_not_verified():
    declared = _entry(_model("vendor/basic", declared=("json",)))
    assert declared["json"]["status"] == "declared_only"
    inferred = _entry(_model("vendor/vision-llava"))
    assert inferred["vision"]["status"] == "inferred_from_name_only"
    assert inferred["vision"]["evidence"] == []


def test_saved_record_is_exposed_with_source_and_timestamp():
    dimensions = _entry(_model(
        evidence=(_record("spanish", "verified", "manual-documentation-2026"),),
    ))
    spanish = dimensions["spanish"]
    assert spanish["status"] == "recorded_verified"
    assert spanish["evidence"][0]["source"] == "manual-documentation-2026"
    assert spanish["evidence"][0]["checked_at"].startswith("2026-10-26")


def test_legacy_migrated_capability_is_not_verified():
    dimensions = _entry(_model(
        declared=("reasoning",),
        evidence=(_record("reasoning", "verified", "migrated_from_v1"),),
    ))
    assert dimensions["reasoning"]["status"] == "legacy_or_incomplete_evidence"
    assert dimensions["reasoning"]["evidence"][0]["legacy_unverified"] is True


def test_unsupported_and_conflicting_records_are_not_claimed_verified():
    unsupported = _entry(_model(evidence=(_record("vision", "unsupported"),)))
    assert unsupported["vision"]["status"] == "recorded_unsupported"
    conflicting = _entry(_model(evidence=(
        _record("code", "verified"),
        _record("coding", "unsupported"),
    )))
    assert conflicting["code"]["status"] == "conflicting_records"


def test_incomplete_and_inconclusive_records_are_not_verified():
    incomplete = _entry(_model(evidence=(
        _record("tools", "verified", checked=""),
    )))
    assert incomplete["tools"]["status"] == "legacy_or_incomplete_evidence"
    inconclusive = _entry(_model(evidence=(
        _record("tools", "inconclusive"),
    )))
    assert inconclusive["tools"]["status"] == "recorded_inconclusive"


def test_unconfigured_providers_and_unknown_dimensions():
    report = capability_report([])
    assert len(report["providers"]) == 4
    assert all(not row["configured"] and not row["models"] for row in report["providers"])
    dimensions = _entry(_model())
    assert dimensions["spanish"]["status"] == "unknown"
    assert dimensions["context"]["status"] == "unknown"
    assert dimensions["documents"]["status"] == "unknown"


def test_admin_endpoint_is_offline_and_never_exposes_credentials(monkeypatch):
    import router.api as api

    provider = _provider([_model(
        "vendor/offline", evidence=(_record("json", "verified"),),
    )])
    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider])
    ))
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "offline-admin")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "offline-app")

    class NoInferenceClient:
        def complete(self, *args, **kwargs):
            pytest.fail("capability report attempted inference")
        def probe_capability(self, *args, **kwargs):
            pytest.fail("capability report attempted a probe")
        def discover_inventory(self, *args, **kwargs):
            pytest.fail("capability report attempted discovery")

    monkeypatch.setattr(api.router, "client", NoInferenceClient(), raising=False)
    client = TestClient(api.app)
    assert client.get("/diagnostics/capabilities").status_code == 401
    assert client.get("/diagnostics/capabilities", headers={
        "Authorization": "Bearer offline-app",
    }).status_code == 401
    response = client.get("/diagnostics/capabilities", headers={
        "Authorization": "Bearer offline-admin",
    })
    assert response.status_code == 200
    assert response.json()["providers"][0]["models"][0]["dimensions"]["json"]["status"] == "recorded_verified"
    assert "offline-placeholder" not in response.text
    assert "offline-admin" not in response.text
