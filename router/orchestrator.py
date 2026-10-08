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
    depends_on: tuple[str, ...] = ()
    critical: bool = False

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
        allowed_caps = {
            "chat","reasoning","json","vision","tools","code","coding",
            "summarization","document","transcription","speech",
            "image_generation","image_editing","long_context","fast",
        }
        subtasks = []
        for index, item in enumerate((data.get("subtasks") or [])[: min(req.max_subtasks, 50)], 1):
            task = str(item.get("task", "")).strip()
            if not task:
                continue
            role = str(item.get("role", "execute")).strip().lower()
            if role not in allowed_roles:
                role = "execute"
            caps = frozenset(str(x) for x in item.get("capabilities", ["chat"]) if str(x) in allowed_caps) or frozenset({"chat"})
            subtask_id = str(item.get("id") or f"s{index}")
            depends_on = tuple(
                str(x) for x in item.get("depends_on", [])
                if isinstance(x, (str, int)) and str(x) != subtask_id
            )
            subtasks.append(PlannedSubtask(
                id=subtask_id,
                role=role,
                task=task,
                capabilities=caps,
                importance=max(0, min(100, int(item.get("importance", 50)))),
                depends_on=depends_on,
                critical=bool(item.get("critical", False)),
            ))
        if not subtasks:
            raise ValueError("empty_plan")
        return TaskPlan(str(data.get("summary", "")).strip() or req.task[:200], tuple(subtasks), bool(data.get("requires_verification", True)))

    def _fallback_plan(self, req: RouteRequest) -> TaskPlan:
        chunks = [x.strip(" -\t") for x in re.split(r"\n+|(?<=[.;])\s+", req.task) if x.strip()]
        chunks = (chunks if len(chunks) > 1 else [req.task])[: min(req.max_subtasks, 50)]
        subtasks = tuple(
            PlannedSubtask(
                f"s{i}",
                "understand" if i == 1 else "execute",
                task,
                req.required_capabilities,
                25 if i == 1 else 50,
                (f"s{i-1}",) if i > 1 else (),
                i == len(chunks),
            )
            for i, task in enumerate(chunks, 1)
        )
        return TaskPlan(req.task[:200], subtasks, True)

    def _budget(self, importance: int) -> tuple[str | None, int | None]:
        # Decomposition exists to turn a hard job into smaller jobs that cheaper,
        # simpler resources can solve. Importance affects the ceiling only slightly;
        # it must not automatically escalate a subtask to a strong/expensive model.
        if importance <= 30:
            return "standard", 20
        if importance <= 65:
            return "standard", 25
        return "standard", 30


    def _planning_request(self, req: RouteRequest) -> RouteRequest:
        schema = {
            "summary": "short summary",
            "requires_verification": True,
            "subtasks": [{
                "id": "s1",
                "role": "understand",
                "task": "self-contained work item",
                "capabilities": ["chat", "reasoning"],
                "importance": 20,
                "depends_on": [],
                "critical": False,
            }],
        }
        task = (
            "Break the task into only the work items that are actually needed. "
            "Use roles from analysis or execution lanes. "
            "Importance is 0-100. Return JSON only. Maximum subtasks: "
            + str(max(1, min(req.max_subtasks, 50)))
            + "\nSchema: " + json.dumps(schema)
            + "\nTask: " + req.task
            + "\nContext: " + req.context
            + "\nRequirements: " + json.dumps(list(req.requirements), ensure_ascii=False)
        )
        return RouteRequest(
            task=task,
            required_capabilities=frozenset({"chat", "json", "reasoning"}),
            preferred_model_class="standard",
            timeout_s=req.timeout_s,
            application_name=req.application_name,
            decompose=False,
            max_strategic_cost=30,
        )

    def _verification_request(self, req: RouteRequest, results: list[dict]) -> RouteRequest:
        task = (
            "Check the partial results against the original task. "
            "Return OK or a concise list of concrete gaps or contradictions."
            + "\nOriginal task: " + req.task
            + "\nResults: " + json.dumps(results, ensure_ascii=False)
        )
        return RouteRequest(
            task=task,
            required_capabilities=frozenset({"chat", "reasoning"}),
            preferred_model_class="standard",
            application_name=req.application_name,
            decompose=False,
            max_strategic_cost=30,
        )

    def _composition_request(self, req: RouteRequest, results: list[dict], verification: str) -> RouteRequest:
        task = (
            "Build one final answer for the original task from the partial results. "
            "Resolve contradictions and satisfy the original requirements. "
            "Return only the final answer."
            + "\nOriginal task: " + req.task
            + "\nRequirements: " + json.dumps(list(req.requirements), ensure_ascii=False)
            + "\nPartial results: " + json.dumps(results, ensure_ascii=False)
            + "\nVerification: " + verification
        )
        return RouteRequest(
            task=task,
            context=req.context,
            requirements=req.requirements,
            required_capabilities=frozenset({"chat", "reasoning"}),
            preferred_model_class="standard",
            timeout_s=req.timeout_s,
            application_name=req.application_name,
            decompose=False,
            max_strategic_cost=30,
        )


    def process(self, req: RouteRequest) -> RouteResponse:
        if not self.should_decompose(req):
            return self.router.route(req, phase="direct")

        planning = self.router.route(self._planning_request(req), phase="plan")
        try:
            plan = self._parse_plan(planning.text or "", req) if planning.ok else self._fallback_plan(req)
        except Exception:
            plan = self._fallback_plan(req)

        all_attempts = list(planning.attempts)
        all_decisions = list(planning.decisions)
        results: list[dict] = []

        completed: dict[str, dict] = {}
        pending = list(plan.subtasks)
        while pending:
            progressed = False
            for subtask in list(pending):
                missing = [dep for dep in subtask.depends_on if dep not in completed]
                if missing:
                    continue
                blocked = [dep for dep in subtask.depends_on if not completed[dep]["ok"]]
                if blocked:
                    item = {
                        "id": subtask.id,
                        "role": subtask.role,
                        "importance": subtask.importance,
                        "critical": subtask.critical,
                        "depends_on": list(subtask.depends_on),
                        "ok": False,
                        "text": None,
                        "provider": None,
                        "model": None,
                        "error": "dependency_failed:" + ",".join(blocked),
                    }
                    results.append(item)
                    completed[subtask.id] = item
                    pending.remove(subtask)
                    progressed = True
                    continue

                dependency_context = [
                    {"id": dep, "text": completed[dep]["text"]}
                    for dep in subtask.depends_on
                    if completed[dep].get("text")
                ]
                model_class, max_cost = self._budget(subtask.importance)
                subreq = RouteRequest(
                    task=subtask.task,
                    context=req.context + (
                        "\nDependency results: " + json.dumps(dependency_context, ensure_ascii=False)
                        if dependency_context else ""
                    ),
                    requirements=req.requirements,
                    required_capabilities=subtask.capabilities,
                    preferred_model_class=model_class,
                    timeout_s=req.timeout_s,
                    application_name=req.application_name,
                    decompose=False,
                    max_strategic_cost=max_cost,
                )
                result = self.router.route(subreq, phase=f"subtask:{subtask.role}")
                all_attempts.extend(result.attempts)
                all_decisions.extend(result.decisions)
                item = {
                    "id": subtask.id,
                    "role": subtask.role,
                    "importance": subtask.importance,
                    "critical": subtask.critical,
                    "depends_on": list(subtask.depends_on),
                    "ok": result.ok,
                    "text": result.text,
                    "provider": result.provider,
                    "model": result.model,
                    "error": result.error,
                }
                results.append(item)
                completed[subtask.id] = item
                pending.remove(subtask)
                progressed = True

            if not progressed:
                for subtask in pending:
                    item = {
                        "id": subtask.id,
                        "role": subtask.role,
                        "importance": subtask.importance,
                        "critical": subtask.critical,
                        "depends_on": list(subtask.depends_on),
                        "ok": False,
                        "text": None,
                        "provider": None,
                        "model": None,
                        "error": "dependency_cycle_or_missing",
                    }
                    results.append(item)
                    completed[subtask.id] = item
                pending.clear()

        failed_critical = [item for item in results if item.get("critical") and not item["ok"]]
        if failed_critical:
            return RouteResponse(
                False, None, None, None, all_attempts, all_decisions,
                "critical_subtask_failed",
                raw={"orchestration": {"plan": plan.summary, "subtasks": results}},
            )

        successful = [item for item in results if item["ok"] and item["text"]]
        if not successful:
            return RouteResponse(
                False, None, None, None, all_attempts, all_decisions,
                "all_subtasks_failed",
                raw={"orchestration": {"plan": plan.summary, "subtasks": results}},
            )

        verification_text = ""
        if plan.requires_verification and len(successful) > 1:
            checked = self.router.route(
                self._verification_request(req, successful),
                phase="verify",
            )
            all_attempts.extend(checked.attempts)
            all_decisions.extend(checked.decisions)
            verification_text = checked.text or ""

        final = self.router.route(
            self._composition_request(req, successful, verification_text),
            phase="compose",
        )
        all_attempts.extend(final.attempts)
        all_decisions.extend(final.decisions)

        trace = {
            "plan": plan.summary,
            "subtasks": results,
            "verification": verification_text,
        }
        if not final.ok:
            trace["composition_fallback"] = True
            fallback_text = "\n\n".join(str(item["text"]) for item in successful)
            return RouteResponse(
                True,
                fallback_text,
                successful[-1]["provider"],
                successful[-1]["model"],
                all_attempts,
                all_decisions,
                raw={"orchestration": trace},
            )

        return RouteResponse(
            True,
            final.text,
            final.provider,
            final.model,
            all_attempts,
            all_decisions,
            raw={"orchestration": trace},
        )
