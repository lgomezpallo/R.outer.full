"""Deterministic fixture-only specialist adapters for offline tests.

Not a production transport. There is no provider selection, network client,
endpoint, credential, callback, or inference execution path in this module.
Importing this module never enables specialist execution in Router.
"""
from __future__ import annotations

from collections.abc import Mapping

from .specialist_contract import (
    SpecialistKind,
    SpecialistRequest,
    SpecialistResult,
    validate_specialist_result,
)

_KINDS = frozenset({"image_generation", "image_editing", "speech", "transcription"})


class OfflineFixtureTransport:
    """Return caller-supplied immutable artifacts, never generated content.

    Fixtures are copied on construction to prevent later mapping changes from
    changing test outcomes. No request prompt or input payload is retained.
    Each method enforces its modality before returning a typed result.
    """

    __slots__ = ("_fixtures",)

    def __init__(self, fixtures: Mapping[SpecialistKind, SpecialistResult]) -> None:
        if not isinstance(fixtures, Mapping):
            raise ValueError("invalid_offline_specialist_fixtures")
        validated: dict[str, SpecialistResult] = {}
        for kind, result in fixtures.items():
            if kind not in _KINDS or not isinstance(result, SpecialistResult) or result.kind != kind:
                raise ValueError("offline_specialist_fixture_kind_mismatch")
            validated[kind] = result
        self._fixtures = validated

    def _fixture(self, request: SpecialistRequest, expected_kind: SpecialistKind) -> SpecialistResult:
        if not isinstance(request, SpecialistRequest) or request.kind != expected_kind:
            raise ValueError("offline_specialist_request_kind_mismatch")
        result = self._fixtures.get(expected_kind)
        if result is None:
            raise LookupError("offline_specialist_fixture_missing")
        return validate_specialist_result(request, result)

    def generate_image(self, request: SpecialistRequest) -> SpecialistResult:
        return self._fixture(request, "image_generation")

    def edit_image(self, request: SpecialistRequest) -> SpecialistResult:
        return self._fixture(request, "image_editing")

    def synthesize_speech(self, request: SpecialistRequest) -> SpecialistResult:
        return self._fixture(request, "speech")

    def transcribe_audio(self, request: SpecialistRequest) -> SpecialistResult:
        return self._fixture(request, "transcription")

    def run_offline(self, request: SpecialistRequest) -> SpecialistResult:
        """Explicit fixture dispatch; does not accept injectable transports."""
        if not isinstance(request, SpecialistRequest):
            raise ValueError("offline_specialist_request_kind_mismatch")
        if request.kind == "image_generation":
            return self.generate_image(request)
        if request.kind == "image_editing":
            return self.edit_image(request)
        if request.kind == "speech":
            return self.synthesize_speech(request)
        if request.kind == "transcription":
            return self.transcribe_audio(request)
        raise ValueError("offline_specialist_request_kind_mismatch")
