"""Offline regressions: orchestration must not discard caller invariants.

A recording router replaces all inference. No network, provider probes or billing.
"""
from router.orchestrator import Orchestrator
from router.types import RouteRequest, RouteResponse


class RecordingRouter:
    def __init__(self):
        self.calls = []

    def route(self, req, phase="execute"):
        self.calls.append((req, phase))
        return RouteResponse(True, "synthetic", "fake", "fake-model", [], [])


def test_explicit_capabilities_are_not_lost_to_planner_or_composition():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Use tools to inspect the provided input",
        required_capabilities=frozenset({"chat", "tools"}),
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert fake.calls == [(req, "direct")]


def test_conversation_history_is_not_dropped_by_decomposition():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Continue the previous conversation",
        conversation=(("user", "prior context"), ("assistant", "prior answer")),
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert fake.calls == [(req, "direct")]


def test_explicit_strategic_ceiling_is_never_relaxed_by_subtask_fallback():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Analyze and summarize a complex task",
        max_strategic_cost=5,
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert fake.calls == [(req, "direct")]
    assert fake.calls[0][0].max_strategic_cost == 5


def test_unconstrained_plain_chat_can_still_decompose():
    fake = RecordingRouter()
    orchestrator = Orchestrator(fake)
    assert orchestrator.should_decompose(RouteRequest(
        task="Analyze and summarize a complex task",
        decompose=True,
    ))
    assert not orchestrator.should_decompose(RouteRequest(
        task="Analyze and summarize a complex task",
        decompose=False,
    ))


def test_automatic_decomposition_does_not_override_capability_or_ceiling():
    orchestrator = Orchestrator(RecordingRouter())
    long_task = "Review this task carefully. " * 60
    assert not orchestrator.should_decompose(RouteRequest(
        task=long_task,
        required_capabilities=frozenset({"chat", "reasoning"}),
    ))
    assert not orchestrator.should_decompose(RouteRequest(
        task=long_task,
        max_strategic_cost=10,
    ))
