"""Offline regressions for code/coding alias affinity; no HTTP or inference."""
from dataclasses import replace

import pytest

from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.task_requirements import analyze_task
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing

CODE_TASK = "Escribí código Python para ordenar una lista"
STAMP = "2026-10-27T12:00:00Z"


def _provider(*, declared: frozenset[str], evidence=()) -> RegisteredProvider:
    generic = ModelSpec(
        "offline/general", frozenset({"chat"}), "standard",
        priority=50, strategic_cost=10, billing_status="free",
    )
    specialist = ModelSpec(
        "offline/specialist", frozenset({"chat"}) | declared, "standard",
        priority=50, strategic_cost=10, billing_status="free",
        evidence=tuple(evidence),
    )
    groq = next(item for item in BUILTINS if item.id == "groq")
    return RegisteredProvider(
        replace(groq, models=(generic, specialist)),
        "synthetic-not-a-real-key", RuntimeState(),
    )


@pytest.mark.parametrize("alias", ["code", "coding"])
def test_verified_code_alias_receives_same_task_affinity(monkeypatch, alias):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    assert "code" in {
        hint["capability"] for hint in analyze_task(CODE_TASK)["suggested_capabilities"]
    }
    provider = _provider(
        declared=frozenset({alias}),
        evidence=(CapabilityEvidence(alias, "verified", "offline-fixture", STAMP),),
    )
    general = rank([provider], RouteRequest("Hola, ¿cómo estás?"))
    requested = rank([provider], RouteRequest(CODE_TASK))
    assert general[0].model == "offline/general"
    assert requested[0].model == "offline/specialist"
    assert "advisory_task_bonus=500" in requested[0].reasons


def test_declared_coding_alias_gets_provisional_bonus_only(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    decisions = rank(
        [_provider(declared=frozenset({"coding"}))], RouteRequest(CODE_TASK)
    )
    specialist = next(item for item in decisions if item.model == "offline/specialist")
    assert "advisory_task_bonus=120" in specialist.reasons
    assert "advisory_task_bonus=500" not in specialist.reasons


def test_unsupported_alias_vetoes_bonus_even_with_verified_other_alias(monkeypatch):
    import router.selector as selector

    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    provider = _provider(
        declared=frozenset({"code", "coding"}),
        evidence=(
            CapabilityEvidence("code", "verified", "offline-fixture", STAMP),
            CapabilityEvidence("coding", "unsupported", "offline-fixture", STAMP),
        ),
    )
    decisions = rank([provider], RouteRequest(CODE_TASK))
    specialist = next(item for item in decisions if item.model == "offline/specialist")
    assert "advisory_task_bonus=0" in specialist.reasons
    assert decisions[0].model == "offline/general"


def test_code_hint_does_not_bypass_hard_capability_or_billing(monkeypatch):
    import router.selector as selector

    provider = _provider(
        declared=frozenset({"coding"}),
        evidence=(CapabilityEvidence("coding", "verified", "offline-fixture", STAMP),),
    )
    # No verified account approval: production billing gate remains default deny.
    assert rank([provider], RouteRequest(CODE_TASK)) == []
    # Even when simulated billing is allowed, missing mandatory vision stays blocked.
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    assert rank(
        [provider],
        RouteRequest(CODE_TASK, required_capabilities=frozenset({"chat", "vision"})),
    ) == []
