from router.orchestrator import Orchestrator
from router.types import RouteRequest, RouteResponse

class FakeRouter:
    def __init__(self):
        self.calls = []

    def route(self, req, phase="execute"):
        self.calls.append((phase, req))
        if phase == "plan":
            return RouteResponse(
                True,
                '{"summary":"plan","requires_verification":true,"subtasks":['
                '{"id":"a","role":"understand","task":"inspect","capabilities":["chat"],"importance":20},'
                '{"id":"b","role":"execute","task":"build","capabilities":["chat","reasoning"],"importance":80}'
                ']}',
                "cheap",
                "planner",
                [],
                [],
            )
        if phase.startswith("subtask:"):
            return RouteResponse(True, f"done:{req.task}", "provider", "model", [], [])
        if phase == "verify":
            return RouteResponse(True, "OK", "provider", "model", [], [])
        if phase == "compose":
            return RouteResponse(True, "FINAL", "provider", "strong-model", [], [])
        return RouteResponse(True, "DIRECT", "provider", "model", [], [])

def test_simple_task_stays_direct():
    fake = FakeRouter()
    result = Orchestrator(fake).process(RouteRequest("hola"))
    assert result.text == "DIRECT"
    assert [phase for phase, _ in fake.calls] == ["direct"]

def test_forced_complex_task_is_decomposed_and_recomposed():
    fake = FakeRouter()
    result = Orchestrator(fake).process(RouteRequest("complex", decompose=True))
    assert result.ok
    assert result.text == "FINAL"
    phases = [phase for phase, _ in fake.calls]
    assert phases == ["plan", "subtask:understand", "subtask:execute", "verify", "compose"]
    assert result.raw["orchestration"]["plan"] == "plan"

def test_subtasks_use_strategic_cost_budgets():
    fake = FakeRouter()
    Orchestrator(fake).process(RouteRequest("complex", decompose=True))
    low = next(req for phase, req in fake.calls if phase == "subtask:understand")
    high = next(req for phase, req in fake.calls if phase == "subtask:execute")
    assert low.max_strategic_cost == 55
    assert low.preferred_model_class == "standard"
    assert high.max_strategic_cost is None
    assert high.preferred_model_class == "strong"


class DependencyRouter(FakeRouter):
    def route(self, req, phase="execute"):
        self.calls.append((phase, req))
        if phase == "plan":
            return RouteResponse(
                True,
                '{"summary":"dep","requires_verification":false,"subtasks":['
                '{"id":"a","role":"understand","task":"first","capabilities":["chat"],"importance":20,"depends_on":[],"critical":false},'
                '{"id":"b","role":"execute","task":"second","capabilities":["chat"],"importance":50,"depends_on":["a"],"critical":true}'
                ']}',
                "provider","planner",[],[],
            )
        if phase == "subtask:understand":
            return RouteResponse(True, "FIRST_RESULT", "p1", "m1", [], [])
        if phase == "subtask:execute":
            assert "FIRST_RESULT" in req.context
            return RouteResponse(True, "SECOND_RESULT", "p2", "m2", [], [])
        if phase == "compose":
            return RouteResponse(True, "FINAL_DEP", "p3", "m3", [], [])
        return RouteResponse(True, "OK", "p", "m", [], [])

def test_dependency_results_are_passed_forward():
    fake = DependencyRouter()
    result = Orchestrator(fake).process(RouteRequest("complex", decompose=True))
    assert result.ok
    assert result.text == "FINAL_DEP"
    phases = [phase for phase, _ in fake.calls]
    assert phases == ["plan", "subtask:understand", "subtask:execute", "compose"]

class CriticalFailureRouter(FakeRouter):
    def route(self, req, phase="execute"):
        self.calls.append((phase, req))
        if phase == "plan":
            return RouteResponse(
                True,
                '{"summary":"critical","requires_verification":false,"subtasks":['
                '{"id":"a","role":"execute","task":"must work","capabilities":["chat"],"importance":90,"depends_on":[],"critical":true}'
                ']}',
                "provider","planner",[],[],
            )
        if phase.startswith("subtask:"):
            return RouteResponse(False, None, None, None, [], [], "failed")
        return RouteResponse(True, "SHOULD_NOT_RUN", "p", "m", [], [])

def test_critical_subtask_failure_stops_composition():
    fake = CriticalFailureRouter()
    result = Orchestrator(fake).process(RouteRequest("complex", decompose=True))
    assert not result.ok
    assert result.error == "critical_subtask_failed"
    assert "compose" not in [phase for phase, _ in fake.calls]
