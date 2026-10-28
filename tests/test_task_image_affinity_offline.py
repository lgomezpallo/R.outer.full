"""Task-aware image creation/editing affinity, with no provider calls or probes."""
from dataclasses import replace

import pytest

from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


def _provider(capability: str, evidence_status: str = "verified") -> RegisteredProvider:
    generic = ModelSpec(
        "offline/general", frozenset({"chat"}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
    )
    specialist = ModelSpec(
        "offline/image-specialist", frozenset({"chat", capability}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
        evidence=(
            CapabilityEvidence(capability, evidence_status, "offline-fixture", "2026-10-27T21:00:00Z"),
        ),
    )
    spec = next(item for item in BUILTINS if item.id == "groq")
    return RegisteredProvider(
        replace(spec, models=(generic, specialist)), "offline-placeholder", RuntimeState()
    )


@pytest.mark.parametrize(("task", "capability"), [
    ("Generá una imagen de una ciudad", "image_generation"),
    ("Create an illustration of a city", "image_generation"),
    ("Retocá esta imagen para mejorar el contraste", "image_editing"),
    ("Edit this image to remove the background", "image_editing"),
])
def test_clear_image_task_prefers_verified_specialist_without_bypassing_chat_gate(
    monkeypatch, task, capability,
):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider(capability)
    assert rank([provider], RouteRequest("Hola"))[0].model == "offline/general"
    decisions = rank([provider], RouteRequest(task))
    assert decisions[0].model == "offline/image-specialist"
    assert "advisory_task_bonus=500" in decisions[0].reasons
    assert decisions[1].model == "offline/general"


def test_unsupported_image_capability_cannot_earn_bonus(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    decisions = rank(
        [_provider("image_generation", evidence_status="unsupported")],
        RouteRequest("Generá una imagen de una ciudad"),
    )
    assert decisions[0].model == "offline/general"
    assert "advisory_task_bonus=0" in decisions[1].reasons


def test_task_hints_never_authorize_unverified_billing():
    assert rank(
        [_provider("image_generation")],
        RouteRequest("Generá una imagen de una ciudad"),
    ) == []


def test_image_only_model_cannot_override_explicit_chat_requirement(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider("image_generation")
    image_only = replace(
        provider.spec.models[1],
        capabilities=frozenset({"image_generation"}),
    )
    provider.spec = replace(provider.spec, models=(provider.spec.models[0], image_only))
    decisions = rank([provider], RouteRequest("Generá una imagen de una ciudad"))
    assert [item.model for item in decisions] == ["offline/general"]
