"""Offline regressions: decomposition must preserve every caller invariant.

All routing is synthetic. No provider calls, probes, billing or network.
"""
import json

from router.orchestrator import Orchestrator
from router.types import RouteRequest, RouteResponse


class RecordingRouter:
    def __init__(self):
        self.calls = []

    def route(self, req, phase="execute"):
        self.calls.append((req, phase))
        if phase == "plan":
            return RouteResponse(
                True,
                json.dumps({
                    "summary": "offline",
                    "requires_verification": True,
                    "subtasks": [
                        {"id": "a", "role": "understand", "task": "inspect", "capabilities": ["chat"], "importance": 20},
                        {"id": "b", "role": "execute", "task": "build", "capabilities": ["chat"], "importance": 80},
                    ],
                }),
                "mock", "mock", [], [],
            )
        return RouteResponse(True, "synthetic", "mock", "mock", [], [])


def test_explicit_capabilities_reach_every_phase_including_composition():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Use tools to inspect the provided input",
        required_capabilities=frozenset({"chat", "tools"}),
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert [phase for _, phase in fake.calls] == [
        "plan", "subtask:understand", "subtask:execute", "verify", "compose"
    ]
    assert all(req.required_capabilities <= call.required_capabilities for call, _ in fake.calls)
    assert {"json", "reasoning"} <= next(
        call.required_capabilities for call, phase in fake.calls if phase == "plan"
    )


def test_history_context_requirements_and_timeout_reach_every_phase():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Continue the previous conversation",
        context="source context",
        conversation=(("user", "prior context"), ("assistant", "prior answer")),
        requirements=("keep citations",),
        timeout_s=17,
        application_name="offline-test",
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert all(call.conversation == req.conversation for call, _ in fake.calls)
    assert all(call.context.startswith(req.context) for call, _ in fake.calls)
    assert all(call.requirements == req.requirements for call, _ in fake.calls)
    assert all(call.timeout_s == 17 and call.application_name == "offline-test" for call, _ in fake.calls)


def test_explicit_strategic_ceiling_bounds_all_phases():
    fake = RecordingRouter()
    req = RouteRequest(
        task="Analyze and summarize a complex task",
        max_strategic_cost=5,
        decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert len(fake.calls) == 5
    assert all(call.max_strategic_cost is not None and call.max_strategic_cost <= 5
               for call, _ in fake.calls)


class CostLimitedRouter(RecordingRouter):
    def route(self, req, phase="execute"):
        if phase.startswith("subtask:"):
            self.calls.append((req, phase))
            if req.max_strategic_cost is not None and req.max_strategic_cost < 35:
                return RouteResponse(False, None, None, None, [], [], "no_eligible_provider")
            return RouteResponse(True, "synthetic", "mock", "mock", [], [])
        return super().route(req, phase)


def test_retry_can_relax_internal_budget_but_never_caller_ceiling():
    fake = CostLimitedRouter()
    req = RouteRequest("complex", max_strategic_cost=40, decompose=True)
    result = Orchestrator(fake).process(req)
    assert result.ok
    subcalls = [call for call, phase in fake.calls if phase.startswith("subtask:")]
    assert [call.max_strategic_cost for call in subcalls] == [20, 40, 30, 40]
    assert all(call.max_strategic_cost is not None and call.max_strategic_cost <= 40
               for call, _ in fake.calls)


def test_no_retry_can_bypass_an_explicit_low_ceiling():
    fake = CostLimitedRouter()
    req = RouteRequest("complex", max_strategic_cost=5, decompose=True)
    result = Orchestrator(fake).process(req)
    assert not result.ok
    assert result.error == "all_subtasks_failed"
    subcalls = [call for call, phase in fake.calls if phase.startswith("subtask:")]
    assert len(subcalls) == 2
    assert all(call.max_strategic_cost == 5 for call in subcalls)


def test_unconstrained_plain_chat_can_still_decompose():
    orchestrator = Orchestrator(RecordingRouter())
    assert orchestrator.should_decompose(RouteRequest("complex", decompose=True))
    assert not orchestrator.should_decompose(RouteRequest("complex", decompose=False))


def test_automatic_decomposition_preserves_special_constraints():
    orchestrator = Orchestrator(RecordingRouter())
    long_task = "Review this task carefully. " * 60
    assert orchestrator.should_decompose(RouteRequest(
        task=long_task, required_capabilities=frozenset({"chat", "reasoning"}),
    ))
    assert orchestrator.should_decompose(RouteRequest(
        task=long_task, max_strategic_cost=10,
    ))


def test_images_remain_direct_even_with_forced_decomposition():
    fake = RecordingRouter()
    req = RouteRequest(
        "describe", images=("data:image/png;base64,AAAA",),
        required_capabilities=frozenset({"chat", "vision"}), decompose=True,
    )
    response = Orchestrator(fake).process(req)
    assert response.ok
    assert fake.calls == [(req, "direct")]


def test_planner_failure_fallback_still_preserves_capabilities_and_ceiling():
    class FailedPlanner(RecordingRouter):
        def route(self, req, phase="execute"):
            if phase == "plan":
                self.calls.append((req, phase))
                return RouteResponse(False, None, None, None, [], [], "no_eligible_provider")
            return super().route(req, phase)

    fake = FailedPlanner()
    req = RouteRequest(
        "inspect.\nimplement.",
        required_capabilities=frozenset({"chat", "tools"}),
        max_strategic_cost=12,
        decompose=True,
    )
    result = Orchestrator(fake).process(req)
    assert result.ok
    assert all(req.required_capabilities <= call.required_capabilities for call, _ in fake.calls)
    assert all(call.max_strategic_cost is not None and call.max_strategic_cost <= 12
               for call, _ in fake.calls)
