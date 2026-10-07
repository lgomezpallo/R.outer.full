from __future__ import annotations
from dataclasses import dataclass
import json
import re
from .types import RouteRequest, RouteResponse

ANALYSIS_ROLES = ("understand", "inspect", "design", "evaluate", "verify", "reserve")
EXECUTION_ROLES = ("plan", "execute", "integrate", "recover", "coordinate", "reserve")

@dataclass(frozen=True)
class PlannedSubtask:
    id: str
    role: str
    task: str
    capabilities: frozenset[str]
    importance: int

@dataclass(frozen=True)
class TaskPlan:
    summary: str
    subtasks: tuple[PlannedSubtask, ...]
    requires_verification: bool = True

class Orchestrator:
    def __init__(self, router) -> None:
        self.router = router

    def should_decompose(self, req: RouteRequest) -> bool:
        if req.decompose is not None:
            return req.decompose
        text = req.task.strip()
        if len(text) >= 900 or len(req.requirements) >= 3:
            return True
        signals = ("analiza y", "revisá y", "compará", "implementá", "armá", "diseñá", "paso a paso", "de punta a punta", "integrá")
        return sum(1 for signal in signals if signal in text.lower()) >= 2

    def _parse_plan(self, text: str, req: RouteRequest) -> TaskPlan:
        start, end = text.find("{"), text.rfind("}")
        raw = text[start:end + 1] if start >= 0 and end > start else text
        data = json.loads(raw)
        allowed_roles = set(ANALYSIS_ROLES + EXECUTION_ROLES)
        allowed_caps = {"chat","reasoning","json","vision","tools","code","summarization","document"}
        subtasks = []
        for index, item in enumerate((data.get("subtasks") or [])[: min(req.max_subtasks, 12)], 1):
            task = str(item.get("task", "")).strip()
            if not task:
                continue
            role = str(item.get("role", "execute")).strip().lower()
            if role not in allowed_roles:
                role = "execute"
            caps = frozenset(str(x) for x in item.get("capabilities", ["chat"]) if str(x) in allowed_caps) or frozenset({"chat"})
            subtasks.append(PlannedSubtask(
                id=str(item.get("id") or f"s{index}"),
                role=role,
                task=task,
                capabilities=caps,
                importance=max(0, min(100, int(item.get("importance", 50)))),
            ))
        if not subtasks:
            raise ValueError("empty_plan")
        return TaskPlan(str(data.get("summary", "")).strip() or req.task[:200], tuple(subtasks), bool(data.get("requires_verification", True)))

    def _fallback_plan(self, req: RouteRequest) -> TaskPlan:
        chunks = [x.strip(" -\t") for x in re.split(r"\n+|(?<=[.;])\s+", req.task) if x.strip()]
        chunks = (chunks if len(chunks) > 1 else [req.task])[: min(req.max_subtasks, 6)]
        subtasks = tuple(PlannedSubtask(f"s{i}", "understand" if i == 1 else "execute", task, req.required_capabilities, 25 if i == 1 else 50) for i, task in enumerate(chunks, 1))
        return TaskPlan(req.task[:200], subtasks, True)

    def _budget(self, importance: int) -> tuple[str | None, int | None]:
        if importance <= 30:
            return "standard", 55
        if importance <= 65:
            return None, 100
        return "strong", None
