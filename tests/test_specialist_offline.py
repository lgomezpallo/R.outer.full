"""Offline specialist adapter fixtures: deterministic, no providers or sockets."""
import socket

import pytest

from router.specialist_contract import (
    SpecialistRequest,
    SpecialistResult,
    specialist_execution_plan,
)
from router.specialist_offline import OfflineFixtureTransport

pytestmark = pytest.mark.strict_billing

REQUESTS = {
    "image_generation": SpecialistRequest("image_generation", "A small tree"),
    "image_editing": SpecialistRequest("image_editing", "Crop", b"input-image", "image/png"),
    "speech": SpecialistRequest("speech", "Hello"),
    "transcription": SpecialistRequest("transcription", input_bytes=b"input-audio", input_media_type="audio/wav"),
}
RESULTS = {
    "image_generation": SpecialistResult("image_generation", b"fixture-png", "image/png"),
    "image_editing": SpecialistResult("image_editing", b"fixture-webp", "image/webp"),
    "speech": SpecialistResult("speech", b"fixture-wav", "audio/wav"),
    "transcription": SpecialistResult("transcription", text="fixture transcript"),
}
METHODS = {
    "image_generation": "generate_image",
    "image_editing": "edit_image",
    "speech": "synthesize_speech",
    "transcription": "transcribe_audio",
}


@pytest.mark.parametrize("kind", list(REQUESTS))
def test_each_offline_adapter_returns_only_matching_typed_fixture(kind):
    adapter = OfflineFixtureTransport(RESULTS)
    request = REQUESTS[kind]
    assert getattr(adapter, METHODS[kind])(request) is RESULTS[kind]
    assert adapter.run_offline(request) is RESULTS[kind]
    assert specialist_execution_plan(request)["execution_allowed"] is False
    assert specialist_execution_plan(request)["account_billing_verified"] is False


@pytest.mark.parametrize("kind", list(REQUESTS))
def test_each_adapter_rejects_cross_modality_requests(kind):
    adapter = OfflineFixtureTransport(RESULTS)
    other = next(k for k in REQUESTS if k != kind)
    with pytest.raises(ValueError, match="offline_specialist_request_kind_mismatch"):
        getattr(adapter, METHODS[kind])(REQUESTS[other])


def test_missing_fixture_fails_closed_without_synthesizing():
    adapter = OfflineFixtureTransport({})
    with pytest.raises(LookupError, match="offline_specialist_fixture_missing"):
        adapter.run_offline(REQUESTS["image_generation"])


def test_fixture_registry_rejects_inconsistent_kinds_and_invalid_mapping():
    with pytest.raises(ValueError, match="offline_specialist_fixture_kind_mismatch"):
        OfflineFixtureTransport({"speech": RESULTS["image_generation"]})
    with pytest.raises(ValueError, match="offline_specialist_fixture_kind_mismatch"):
        OfflineFixtureTransport({"unsupported": RESULTS["speech"]})
    with pytest.raises(ValueError, match="invalid_offline_specialist_fixtures"):
        OfflineFixtureTransport(None)


def test_fixture_mapping_is_snapshotted_and_requests_are_not_stored():
    fixtures = dict(RESULTS)
    adapter = OfflineFixtureTransport(fixtures)
    fixtures["speech"] = RESULTS["image_generation"]
    assert adapter.run_offline(REQUESTS["speech"]) is RESULTS["speech"]
    assert not hasattr(adapter, "requests")
    assert "input-image" not in repr(adapter)


def test_no_socket_or_network_required_even_when_network_is_forbidden(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("offline specialist adapter attempted network access")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    adapter = OfflineFixtureTransport(RESULTS)
    for kind, request in REQUESTS.items():
        assert adapter.run_offline(request) is RESULTS[kind]


def test_dispatch_rejects_non_requests():
    adapter = OfflineFixtureTransport(RESULTS)
    with pytest.raises(ValueError, match="offline_specialist_request_kind_mismatch"):
        adapter.run_offline(object())
