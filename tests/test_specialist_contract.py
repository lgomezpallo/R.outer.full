"""Offline specialist artifact contracts: no HTTP, credentials, or inference."""
import pytest

from router.specialist_contract import (
    SpecialistRequest,
    SpecialistResult,
    specialist_execution_plan,
    validate_specialist_result,
)
from router.modality_contract import completion_transport_error
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize("kind,prompt,payload,mime", [
    ("image_generation", "Draw a tree", None, None),
    ("image_editing", "Remove background", b"image", "image/png"),
    ("speech", "Read this", None, None),
    ("transcription", "", b"sound", "audio/wav"),
])
def test_typed_request_and_plan_never_authorize_execution(kind, prompt, payload, mime):
    req = SpecialistRequest(kind, prompt, payload, mime)
    plan = specialist_execution_plan(req)
    assert plan["kind"] == kind
    assert plan["required_capability"] == kind
    assert plan["transport"] == "not_implemented"
    assert plan["execution_allowed"] is False
    assert plan["account_billing_verified"] is False
    assert plan["reason"] == "specialist_transport_not_implemented"
    # The current completion transport must still fail closed.
    assert completion_transport_error(
        RouteRequest(prompt or "Transcribe", required_capabilities=frozenset({kind}))
    ) == f"unsupported_completion_modality:{kind}"


@pytest.mark.parametrize("kind", ["image_generation", "image_editing", "speech"])
def test_non_transcription_requires_prompt(kind):
    payload = b"img" if kind == "image_editing" else None
    mime = "image/jpeg" if payload else None
    with pytest.raises(ValueError, match="specialist_prompt_required"):
        SpecialistRequest(kind, "  ", payload, mime)


@pytest.mark.parametrize("kind", ["image_editing", "transcription"])
def test_input_required_for_edit_and_transcription(kind):
    with pytest.raises(ValueError, match="invalid_specialist_input_bytes"):
        SpecialistRequest(kind, "edit" if kind == "image_editing" else "")


def test_mime_type_cannot_cross_image_audio_boundary():
    with pytest.raises(ValueError, match="invalid_specialist_input_media_type"):
        SpecialistRequest("image_editing", "edit", b"audio", "audio/wav")
    with pytest.raises(ValueError, match="invalid_specialist_input_media_type"):
        SpecialistRequest("transcription", "", b"image", "image/png")


def test_generation_rejects_source_artifact_and_excessive_input():
    with pytest.raises(ValueError, match="specialist_input_not_supported"):
        SpecialistRequest("image_generation", "draw", b"image", "image/png")
    with pytest.raises(ValueError, match="invalid_specialist_input_bytes"):
        SpecialistRequest("image_editing", "edit", b"x" * (10 * 1024 * 1024 + 1), "image/png")


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan"), float("inf")])
def test_timeout_is_bounded(timeout):
    with pytest.raises(ValueError, match="invalid_specialist_timeout"):
        SpecialistRequest("speech", "say hello", timeout_s=timeout)


def test_unknown_kind_is_rejected_at_runtime():
    with pytest.raises(ValueError, match="unsupported_specialist_kind"):
        SpecialistRequest("video_generation", "draw")


@pytest.mark.parametrize("kind,media", [
    ("image_generation", "image/png"),
    ("image_editing", "image/webp"),
    ("speech", "audio/mpeg"),
])
def test_binary_results_are_typed_and_accepted(kind, media):
    request = SpecialistRequest(
        kind, "make", b"source" if kind == "image_editing" else None,
        "image/png" if kind == "image_editing" else None,
    )
    result = SpecialistResult(kind, output_bytes=b"synthetic", output_media_type=media)
    assert validate_specialist_result(request, result) is result


def test_transcription_result_is_text_only():
    request = SpecialistRequest("transcription", input_bytes=b"sound", input_media_type="audio/ogg")
    result = SpecialistResult("transcription", text="transcribed words")
    assert validate_specialist_result(request, result) is result
    with pytest.raises(ValueError, match="unexpected_transcription_artifact"):
        SpecialistResult("transcription", text="words", output_bytes=b"audio", output_media_type="audio/wav")


def test_result_mismatch_and_mime_confusion_fail_closed():
    request = SpecialistRequest("speech", "say hello")
    image = SpecialistResult("image_generation", output_bytes=b"png", output_media_type="image/png")
    with pytest.raises(ValueError, match="specialist_result_kind_mismatch"):
        validate_specialist_result(request, image)
    with pytest.raises(ValueError, match="invalid_specialist_output_media_type"):
        SpecialistResult("speech", output_bytes=b"png", output_media_type="image/png")
    with pytest.raises(ValueError, match="unexpected_specialist_text"):
        SpecialistResult("speech", output_bytes=b"wav", output_media_type="audio/wav", text="oops")


def test_empty_and_oversized_outputs_are_rejected():
    with pytest.raises(ValueError, match="invalid_specialist_output_bytes"):
        SpecialistResult("image_generation", output_bytes=b"", output_media_type="image/png")
    with pytest.raises(ValueError, match="invalid_specialist_output_bytes"):
        SpecialistResult("image_generation", output_bytes=b"x" * (20 * 1024 * 1024 + 1), output_media_type="image/png")
    with pytest.raises(ValueError, match="invalid_transcription_text"):
        SpecialistResult("transcription", text="   ")
