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
