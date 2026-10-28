"""Typed, offline-only contracts for future specialist image/audio transports.

This module deliberately does not contain a network client, endpoint URL,
provider credentials, or an execution function. It cannot authorize usage.
Existing chat completion continues to reject specialist capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

SpecialistKind = Literal["image_generation", "image_editing", "speech", "transcription"]

_IMAGE_INPUTS = frozenset({"image/png", "image/jpeg", "image/webp"})
_AUDIO_INPUTS = frozenset({"audio/wav", "audio/mpeg", "audio/ogg", "audio/webm", "audio/mp4"})
_IMAGE_OUTPUTS = _IMAGE_INPUTS
_AUDIO_OUTPUTS = frozenset({"audio/wav", "audio/mpeg", "audio/ogg"})
_KINDS = frozenset({"image_generation", "image_editing", "speech", "transcription"})
_MAX_INPUT_BYTES = 10 * 1024 * 1024
_MAX_OUTPUT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class SpecialistRequest:
    """A single artifact job. Payloads stay in memory, never in routing logs."""

    kind: SpecialistKind
    prompt: str = ""
    input_bytes: bytes | None = None
    input_media_type: str | None = None
    timeout_s: float = 45.0

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError("unsupported_specialist_kind")
        if not isinstance(self.prompt, str) or len(self.prompt) > 20_000:
            raise ValueError("invalid_specialist_prompt")
        if self.kind != "transcription" and not self.prompt.strip():
            raise ValueError("specialist_prompt_required")
        if not isinstance(self.timeout_s, (int, float)) or not (0 < self.timeout_s <= 120):
            raise ValueError("invalid_specialist_timeout")
        if self.kind in {"image_generation", "speech"}:
            if self.input_bytes is not None or self.input_media_type is not None:
                raise ValueError("specialist_input_not_supported")
            return
        allowed = _IMAGE_INPUTS if self.kind == "image_editing" else _AUDIO_INPUTS
        if (not isinstance(self.input_bytes, bytes) or not self.input_bytes
                or len(self.input_bytes) > _MAX_INPUT_BYTES):
            raise ValueError("invalid_specialist_input_bytes")
        if self.input_media_type not in allowed:
            raise ValueError("invalid_specialist_input_media_type")


@dataclass(frozen=True)
class SpecialistResult:
    """Typed artifact response; transcription returns text, not binary audio."""

    kind: SpecialistKind
    output_bytes: bytes | None = None
    output_media_type: str | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError("unsupported_specialist_kind")
        if self.kind == "transcription":
            if (not isinstance(self.text, str) or not self.text.strip()
                    or len(self.text) > 100_000):
                raise ValueError("invalid_transcription_text")
            if self.output_bytes is not None or self.output_media_type is not None:
                raise ValueError("unexpected_transcription_artifact")
            return
        if self.text is not None:
            raise ValueError("unexpected_specialist_text")
        if (not isinstance(self.output_bytes, bytes) or not self.output_bytes
                or len(self.output_bytes) > _MAX_OUTPUT_BYTES):
            raise ValueError("invalid_specialist_output_bytes")
        allowed = _AUDIO_OUTPUTS if self.kind == "speech" else _IMAGE_OUTPUTS
        if self.output_media_type not in allowed:
            raise ValueError("invalid_specialist_output_media_type")


class SpecialistTransport(Protocol):
    """Future adapter boundary; no concrete implementation is shipped."""

    def generate_image(self, request: SpecialistRequest) -> SpecialistResult: ...
    def edit_image(self, request: SpecialistRequest) -> SpecialistResult: ...
    def synthesize_speech(self, request: SpecialistRequest) -> SpecialistResult: ...
    def transcribe_audio(self, request: SpecialistRequest) -> SpecialistResult: ...


def validate_specialist_result(request: SpecialistRequest, result: SpecialistResult) -> SpecialistResult:
    """Reject an adapter returning the wrong modality before exposing an artifact."""
    if not isinstance(result, SpecialistResult) or result.kind != request.kind:
        raise ValueError("specialist_result_kind_mismatch")
    return result


def specialist_execution_plan(request: SpecialistRequest) -> dict[str, str | bool]:
    """Describe an offline job without dispatching or implying account authorization."""
    return {
        "kind": request.kind,
        "required_capability": request.kind,
        "transport": "not_implemented",
        "execution_allowed": False,
        "account_billing_verified": False,
        "reason": "specialist_transport_not_implemented",
    }
