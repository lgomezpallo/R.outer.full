"""Offline regression tests: generic task hints affect ranking, not authorization."""
from dataclasses import replace

import pytest

from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


def _provider(capability: str, status: str = "verified") -> RegisteredProvider:
    generic = ModelSpec(
        "offline/general", frozenset({"chat"}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
    )
    specialist = ModelSpec(
        "offline/specialist", frozenset({"chat", capability}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
        evidence=(CapabilityEvidence(capability, status, "offline-fixture", "2026-10-27T12:00:00Z"),),
    )
    spec = next(item for item in BUILTINS if item.id == "groq")
    return RegisteredProvider(
        replace(spec, models=(generic, specialist)), "synthetic-not-a-real-key", RuntimeState()
    )


@pytest.mark.parametrize(("task", "capability"), [
    ("Respondé en JSON con dos claves", "json"),
    ("Usá herramientas para resolver esta tarea", "tools"),
    ("Resumí este texto en tres frases", "summarization"),
    ("Razoná paso a paso este problema matemático", "reasoning"),
    ("Analizá esta imagen", "vision"),
    ("Resumí este PDF", "document"),
])
def test_explicit_task_prefers_verified_specialist_offline(monkeypatch, task, capability):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider(capability)
    general = rank([provider], RouteRequest("Hola, ¿cómo estás?"))
    requested = rank([provider], RouteRequest(task))
    assert [item.model for item in general] == ["offline/general", "offline/specialist"]
    assert [item.model for item in requested] == ["offline/specialist", "offline/general"]
    assert "advisory_task_bonus=500" in requested[0].reasons


@pytest.mark.parametrize(("task", "capability"), [
    ("Respondé en JSON", "json"),
    ("Usá herramientas", "tools"),
    ("Resumí este texto", "summarization"),
])
def test_unsupported_evidence_does_not_earn_advisory_bonus(monkeypatch, task, capability):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    decisions = rank([_provider(capability, "unsupported")], RouteRequest(task))
    assert decisions[0].model == "offline/general"
    assert "advisory_task_bonus=0" in decisions[1].reasons


def test_generic_hints_do_not_override_hard_requirements(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider("json")
    req = RouteRequest("Respondé en JSON", required_capabilities=frozenset({"chat", "vision"}))
    assert rank([provider], req) == []


def test_task_hints_never_authorize_unverified_account():
    for capability, task in (
        ("json", "Respondé en JSON"),
        ("tools", "Usá herramientas"),
        ("summarization", "Resumí este texto"),
    ):
        provider = _provider(capability)
        provider.spec = replace(provider.spec, models=tuple(
            replace(model, billing_status="unknown") for model in provider.spec.models
        ))
        assert rank([provider], RouteRequest(task)) == []


def test_quoted_json_request_is_not_ranking_instruction(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    decisions = rank([_provider("json")], RouteRequest("> Respondé en JSON\nHola"))
    assert decisions[0].model == "offline/general"
