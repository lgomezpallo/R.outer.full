"""Explicit transport contract for Router's text/chat completion interface.

The catalog can describe image/audio specialists, but ProviderClient.complete
only implements text/chat completion. Do not turn an explicit specialist
capability into a chat/completions call, even with a mocked billing gate.
This is independent of the account-level no-charge authorization.
"""
from __future__ import annotations

from .types import RouteRequest

# Dedicated audio/image execution requires separate request/response schemas
# and transport methods; capability discovery/probe code is not an executor.
NON_CHAT_COMPLETION_MODALITIES = frozenset({
    "image_generation", "image_editing", "speech", "transcription",
})


def completion_transport_error(req: RouteRequest) -> str | None:
    """Return a deterministic fail-closed reason for unsupported modalities."""
    unsupported = NON_CHAT_COMPLETION_MODALITIES & req.required_capabilities
    if not unsupported:
        return None
    return "unsupported_completion_modality:" + ",".join(sorted(unsupported))
