"""Offline regression: cheap-first retry must not erase routing telemetry.

All provider responses are synthetic. No network, probes, billing or inference.
"""
from router.orchestrator import Orchestrator
from router.types import Attempt, Decision, RouteRequest, RouteResponse


class TraceRouter:
    def __init__(self, retry_succeeds=True):
        self.calls = []
        self.retry_succeeds = retry_succeeds

    def route(self, req, phase="execute"):
        self.calls.append((phase, req.max_strategic_cost))
        if phase == "plan":
            return RouteResponse(
                True,
                '{"summary":"offline","requires_verification":false,"subtasks":['
                '{"id":"s1","role":"execute","task":"work","capabilities":["chat"],'
                '"importance":20,"critical":true}]}',
                "mock", "planner", [], [],
            )
        if phase.startswith("subtask:"):
            cheap_first = req.max_strategic_cost == 20
            ok = not cheap_first and self.retry_succeeds
            model = "cheap-candidate" if cheap_first else "retry-candidate"
            return RouteResponse(
                ok, "synthetic" if ok else None,
                "mock" if ok else None, model if ok else None,
                [Attempt("mock", model, ok, 1, None if ok else "not_eligible", phase)],
                [Decision("mock", model, 0.5, ["synthetic"])],
                None if ok else "no_eligible_provider",
            )
        if phase == "compose":
            return RouteResponse(True, "FINAL", "mock", "composer", [], [])
        raise AssertionError(f"Unexpected phase {phase}")


def test_successful_retry_preserves_both_attempts_and_decisions():
    fake = TraceRouter()
    result = Orchestrator(fake).process(
        RouteRequest("complex", max_strategic_cost=40, decompose=True)
    )
    assert result.ok and result.text == "FINAL"
    assert fake.calls == [
        ("plan", 30), ("subtask:execute", 20),
        ("subtask:execute", 40), ("compose", 30),
    ]
    assert [a.model for a in result.attempts] == [
        "cheap-candidate", "retry-candidate"
    ]
    assert [d.model for d in result.decisions] == [
        "cheap-candidate", "retry-candidate"
    ]


def test_failed_retry_preserves_both_traces_before_critical_failure():
    fake = TraceRouter(retry_succeeds=False)
    result = Orchestrator(fake).process(
        RouteRequest("complex", max_strategic_cost=40, decompose=True)
    )
    assert not result.ok and result.error == "critical_subtask_failed"
    assert [a.model for a in result.attempts] == [
        "cheap-candidate", "retry-candidate"
    ]
    assert [d.model for d in result.decisions] == [
        "cheap-candidate", "retry-candidate"
    ]
    assert ("compose", 30) not in fake.calls


def test_explicit_low_ceiling_has_no_retry_or_duplicate_trace():
    fake = TraceRouter()
    result = Orchestrator(fake).process(
        RouteRequest("complex", max_strategic_cost=10, decompose=True)
    )
    assert not result.ok and result.error == "critical_subtask_failed"
    assert fake.calls == [("plan", 10), ("subtask:execute", 10)]
    assert len(result.attempts) == 1
    assert len(result.decisions) == 1
