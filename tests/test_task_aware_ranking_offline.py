"""Offline task-aware model ranking: hints steer preference, never authorize calls."""
from dataclasses import replace

import pytest

pytestmark = pytest.mark.strict_billing

from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.types import RouteRequest


def _models():
    generic = ModelSpec(
        "vendor/offline-general", frozenset({"chat"}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
    )
    specialist = ModelSpec(
        "vendor/offline-coder", frozenset({"chat", "code"}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
        evidence=(CapabilityEvidence("code", "verified", "offline-test", "2026-10-27T12:00:00Z"),),
    )
    return generic, specialist


def _provider():
    spec = next(item for item in BUILTINS if item.id == "groq")
    return RegisteredProvider(
        replace(spec, models=_models()), "synthetic-not-a-real-key", RuntimeState()
    )


def test_code_task_prefers_verified_specialist_without_hard_requirement(monkeypatch):
    import router.selector as selector
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider()
    general = rank([provider], RouteRequest("Hola, ¿cómo estás?"))
    coding = rank([provider], RouteRequest("Escribí código en Python para ordenar datos"))
    assert [item.model for item in general] == ["vendor/offline-general", "vendor/offline-coder"]
    assert [item.model for item in coding] == ["vendor/offline-coder", "vendor/offline-general"]
    assert any("advisory_task_bonus=500" in reason for reason in coding[0].reasons)


def test_quoted_or_fenced_instructions_do_not_trigger_task_affinity(monkeypatch):
    import router.selector as selector
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider()
    for prompt in ("> Escribí código en Python\nHola", "Hola\n```\nEscribí código en Python\n```"):
        decisions = rank([provider], RouteRequest(prompt))
        assert decisions[0].model == "vendor/offline-general"


def test_task_hint_does_not_bypass_unverified_account_or_require_code():
    provider = _provider()
    # Production billing policy remains fail-closed even for the preferred specialist.
    assert rank([provider], RouteRequest("Escribí código en Python")) == []
    # A missing specialist does not make the hint a hard requirement.
    from router.task_requirements import analyze_task
    analysis = analyze_task("Escribí código en Python")
    assert analysis["explicit_required_capabilities"] == ["chat"]
    assert analysis["routing_modified"] is False
