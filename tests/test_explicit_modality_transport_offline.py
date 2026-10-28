"""Explicit image/audio modality contracts are never sent as chat completions.

All tests are synthetic and require no credentials or provider network access.
"""
from dataclasses import replace

import pytest

from router.client import ProviderClient
from router.modality_contract import completion_transport_error
from router.ranking_diagnostics import ranking_report
from router.router import Router
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize("modality", [
    "image_generation", "image_editing", "speech", "transcription",
])
def test_direct_route_rejects_specialist_execution_before_client_call(modality):
    class ExplodingClient:
        def complete(self, *_args, **_kwargs):
            raise AssertionError("must not call a chat transport")

    router = Router(client=ExplodingClient())
    request = RouteRequest(
        "Create an image or audio artifact",
        required_capabilities=frozenset({modality}),
    )
    result = router.route(request)
    assert not result.ok
    assert result.error == f"unsupported_completion_modality:{modality}"
    assert result.attempts == []
    assert result.decisions == []


def test_process_rejects_before_orchestration_planning():
    router = Router()
    router.orchestrator.process = lambda *_: pytest.fail("orchestration must not run")
    req = RouteRequest(
        "Design a picture, then edit it",
        required_capabilities=frozenset({"image_generation", "image_editing"}),
        decompose=True,
    )
    result = router.process(req)
    assert result.error == "unsupported_completion_modality:image_editing,image_generation"
    assert not result.attempts


def test_direct_client_complete_cannot_use_chat_transport_for_image():
    # No provider is needed: the guard must run before billing and transport.
    with pytest.raises(RuntimeError, match="unsupported_completion_modality:image_generation"):
        ProviderClient().complete(
            None, "offline-specialist",
            RouteRequest("Draw", required_capabilities=frozenset({"image_generation"})),
        )


def test_chat_vision_remains_supported_by_transport_contract():
    req = RouteRequest("Describe this photo", required_capabilities=frozenset({"chat", "vision"}))
    assert completion_transport_error(req) is None


def test_preview_marks_specialist_execution_as_unsupported_even_without_models():
    report = ranking_report(
        [], "Generate an image",
        required_capabilities=frozenset({"image_generation"}),
    )
    assert report["execution_transport_supported"] is False
    assert report["execution_transport_blocker"] == "unsupported_completion_modality:image_generation"
    assert report["inference_performed"] is False
    assert report["account_billing_verified"] is False


def test_chat_preview_keeps_transport_supported_but_not_billing_authorized():
    report = ranking_report([], "Hello")
    assert report["execution_transport_supported"] is True
    assert report["execution_transport_blocker"] is None
    assert report["account_billing_verified"] is False
