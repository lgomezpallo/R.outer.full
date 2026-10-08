from __future__ import annotations
import json
from time import perf_counter
from uuid import uuid4
import httpx
from .catalog import ProviderCatalog
from .client import ProviderClient
from .orchestrator import Orchestrator
from .registry import ProviderRegistry, RegisteredProvider
from .selector import rank
from .storage import RouterStore
from .types import Attempt, ProviderCredential, RouteRequest, RouteResponse

class Router:
    def __init__(
        self,
        client: ProviderClient | None = None,
        max_retries: int = 1,
        catalog: ProviderCatalog | None = None,
        store: RouterStore | None = None,
    ) -> None:
        self.registry = ProviderRegistry(catalog)
        self.catalog = self.registry.catalog
        self.client = client or ProviderClient()
        self.max_retries = max(0, max_retries)
        self.store = store
        self.orchestrator = Orchestrator(self)

    def add_provider(
        self,
        name: str,
        api_key: str,
        *,
        persist: bool = False,
        discover: bool = True,
    ) -> RegisteredProvider:
        registered = self.registry.add(ProviderCredential(name, api_key))
        if persist and self.store is not None:
            self.store.save_credential(registered.spec.id, api_key)
        if discover and registered.spec.discover_models and hasattr(self.client, "discover_models"):
            try:
                model_ids = self.client.discover_models(registered)
                if model_ids:
                    registered.spec = self.catalog.merge_discovered_models(registered.spec.id, model_ids)
            except Exception:
                pass
        self._hydrate_provider_metrics(registered)
        return registered

    def load_persisted_providers(self) -> int:
        if self.store is None:
            return 0
        loaded = 0
        for credential in self.store.load_credentials():
            try:
                self.add_provider(credential.provider_id, credential.api_key, persist=False, discover=True)
                loaded += 1
            except ValueError:
                continue
        return loaded

    def _hydrate_provider_metrics(self, provider: RegisteredProvider) -> None:
        if self.store is None:
            return
        runtime = self.store.load_provider_runtime(provider.spec.id)
        if runtime:
            provider.state.success_count = int(runtime["success_count"])
            provider.state.failure_count = int(runtime["failure_count"])
            provider.state.ewma_latency_ms = runtime["ewma_latency_ms"]
            provider.state.cooldown_until = float(runtime["cooldown_until"] or 0)
            provider.state.last_error = runtime["last_error"]
            provider.state.health_ok = runtime["health_ok"]
            provider.state.cooldown_strikes = int(runtime.get("cooldown_strikes", 0) or 0)
            provider.state.last_failure_kind = runtime.get("last_failure_kind")
        stats = self.store.recent_model_stats()
        for model in provider.spec.models:
            item = stats.get((provider.spec.id, model.id))
            if not item:
                continue
            state = provider.state.model(model.id)
            samples = max(1, int(item["samples"]))
            successes = max(0, min(samples, round(float(item["success_rate"]) * samples)))
            state.success_count = successes
            state.failure_count = samples - successes
            state.ewma_latency_ms = float(item["latency_ms"])

    def process(self, req: RouteRequest) -> RouteResponse:
        return self.orchestrator.process(req)

    def _classify_failure(self, exc: Exception) -> tuple[bool, bool, str, int | None, float]:
        retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
        provider_wide = retryable
        error_type = "unreachable" if isinstance(exc, httpx.TransportError) else "provider_error"
        error_code: int | None = None
        cooldown = 60.0 if retryable else 0.0

        if isinstance(exc, httpx.TimeoutException):
            error_type = "timeout"
            error_code = 408
            cooldown = 60.0

        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            error_code = status
            retryable = status in {400, 401, 402, 403, 404, 405, 408, 409, 413, 415, 422, 429, 500, 502, 503, 504}
            provider_wide = status in {401, 402, 403, 408, 429, 500, 502, 503, 504}
            if status == 429:
                error_type, cooldown = "quota", 15 * 60.0
            elif status == 402:
                error_type, cooldown = "payment", 30 * 60.0
            elif status in {408, 504}:
                error_type, cooldown = "timeout", 60.0
            elif status >= 500:
                error_type, cooldown = "server", 2 * 60.0
            elif status in {401, 403}:
                error_type, cooldown = "rejected", 10 * 60.0
            else:
                error_type, cooldown = "invalid", 2 * 60.0

        return retryable, provider_wide, error_type, error_code, cooldown

    def route(self, req: RouteRequest, phase: str = "execute") -> RouteResponse:
        decisions = rank(self.registry.all(), req)
        if not decisions:
            return RouteResponse(False, None, None, None, [], [], "no_eligible_provider")

        request_id = str(uuid4())
        attempts: list[Attempt] = []
        by_id = {p.spec.id: p for p in self.registry.all()}
        blocked_providers: set[str] = set()

        for decision in decisions:
            if decision.provider in blocked_providers:
                continue
            provider = by_id[decision.provider]
            for attempt_index in range(self.max_retries + 1):
                started = perf_counter()
                try:
                    text, raw = self.client.complete(provider, decision.model, req)
                    latency = int((perf_counter() - started) * 1000)
                    provider.state.mark_success(decision.model, latency)
                    if self.store is not None:
                        self.store.save_provider_runtime(provider.spec.id, provider.state)
                    attempts.append(Attempt(decision.provider, decision.model, True, latency, phase=phase))
                    if self.store is not None:
                        self.store.record_metric(
                            request_id=request_id,
                            application_name=req.application_name,
                            provider_id=decision.provider,
                            model_id=decision.model,
                            phase=phase,
                            attempt_number=len(attempts),
                            latency_ms=latency,
                            success=True,
                        )
                    return RouteResponse(True, text, decision.provider, decision.model, attempts, decisions, raw=raw)
                except (httpx.TimeoutException, httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError, ValueError) as exc:
                    latency = int((perf_counter() - started) * 1000)
                    error = f"{type(exc).__name__}: {exc}"
                    retryable, provider_wide, error_type, error_code, cooldown = self._classify_failure(exc)
                    attempts.append(Attempt(decision.provider, decision.model, False, latency, error, phase))
                    if self.store is not None:
                        self.store.record_metric(
                            request_id=request_id,
                            application_name=req.application_name,
                            provider_id=decision.provider,
                            model_id=decision.model,
                            phase=phase,
                            attempt_number=len(attempts),
                            latency_ms=latency,
                            success=False,
                            error_type=error_type,
                            error_code=error_code,
                        )
                    if retryable and attempt_index < self.max_retries:
                        continue
                    provider.state.mark_failure(
                        decision.model,
                        error,
                        cooldown,
                        failure_kind=error_type,
                    )
                    if self.store is not None:
                        self.store.save_provider_runtime(provider.spec.id, provider.state)
                    if provider_wide:
                        blocked_providers.add(decision.provider)
                    break

        return RouteResponse(False, None, None, None, attempts, decisions, "all_attempts_failed")

    def verify_capability(self, provider_id: str, model_id: str, capability: str) -> dict:
        provider = next((item for item in self.registry.all() if item.spec.id == provider_id), None)
        if provider is None:
            raise ValueError("provider_not_registered")
        if not any(model.id == model_id for model in provider.spec.models):
            raise ValueError("model_not_cataloged")

        started = perf_counter()
        if hasattr(self.client, "probe_capability"):
            try:
                probe = self.client.probe_capability(provider, model_id, capability)
                status = str(probe.get("status", "inconclusive"))
                if status not in {"verified", "unsupported", "inconclusive"}:
                    status = "inconclusive"
                evidence = str(probe.get("evidence", "active_probe"))[:500]
                http_status = probe.get("http_status")
            except Exception as exc:
                status = "inconclusive"
                evidence = f"probe_error:{type(exc).__name__}"
                http_status = None
        else:
            probes = {
                "chat": ("Reply with exactly ROUTER_OK", lambda text: text.strip() == "ROUTER_OK"),
                "json": ('Return exactly this JSON object: {"router_ok":true}', lambda text: json.loads(text).get("router_ok") is True),
            }
            if capability not in probes:
                status = "inconclusive"
                evidence = "no_specific_probe_defined"
                http_status = None
            else:
                prompt, validator = probes[capability]
                req = RouteRequest(
                    task=prompt,
                    required_capabilities=frozenset({"chat"}),
                    decompose=False,
                    application_name="router-capability-probe",
                    timeout_s=20,
                )
                try:
                    text, _ = self.client.complete(provider, model_id, req)
                    ok = bool(validator(text))
                    status = "verified" if ok else "unsupported"
                    evidence = "specific_probe_passed" if ok else "specific_probe_failed"
                except Exception as exc:
                    status = "inconclusive"
                    evidence = f"probe_error:{type(exc).__name__}"
                http_status = None

        latency = int((perf_counter() - started) * 1000)
        updated = self.catalog.record_capability(provider_id, model_id, capability, status, evidence)
        provider.spec = updated
        result = {
            "provider": provider_id,
            "model": model_id,
            "capability": capability,
            "status": status,
            "latency_ms": latency,
            "evidence": evidence,
        }
        if isinstance(http_status, int):
            result["http_status"] = http_status
        return result

    def audit_provider_capabilities(self, provider_id: str, *, inconclusive_only: bool = False) -> dict:
        provider = next((item for item in self.registry.all() if item.spec.id == provider_id), None)
        if provider is None:
            raise ValueError("provider_not_registered")
        checks: list[dict] = []
        for model in provider.spec.models:
            existing = {item.capability: item.status for item in model.evidence}
            for capability in sorted(model.capabilities):
                if inconclusive_only and existing.get(capability) != "inconclusive":
                    continue
                checks.append(self.verify_capability(provider_id, model.id, capability))
        return {
            "provider": provider_id,
            "checks": checks,
            "verified": sum(1 for item in checks if item["status"] == "verified"),
            "unsupported": sum(1 for item in checks if item["status"] == "unsupported"),
            "inconclusive": sum(1 for item in checks if item["status"] == "inconclusive"),
        }


    def test_provider(self, provider_id: str) -> dict:
        provider = next((item for item in self.registry.all() if item.spec.id == provider_id), None)
        if provider is None:
            raise ValueError("provider_not_registered")
        candidates = [
            model for model in provider.spec.models
            if "chat" in model.capabilities and "chat" not in model.unsupported_capabilities
        ]
        if not candidates:
            return {
                "provider": provider_id,
                "status": "inconclusive",
                "reason": "no_chat_model",
                "health_status": provider.state.health_status,
                "checked": [],
            }
        candidates.sort(key=lambda model: (model.strategic_cost, -model.priority, model.id))
        checked: list[dict] = []
        best = None
        for model in candidates[:8]:
            result = self.verify_capability(provider_id, model.id, "chat")
            checked.append({
                "model": model.id,
                "status": result["status"],
                "latency_ms": result.get("latency_ms"),
                "http_status": result.get("http_status"),
                "evidence": result.get("evidence"),
            })
            if result["status"] == "verified":
                provider.state.mark_success(model.id, int(result.get("latency_ms", 0)))
                best = result
                break
            provider.state.mark_failure(
                model.id,
                result.get("evidence", result["status"]),
                60.0 if result["status"] == "inconclusive" else 120.0,
                failure_kind="provider_test",
            )
        if self.store is not None:
            self.store.save_provider_runtime(provider_id, provider.state)
        if best is None:
            last = checked[-1]
            return {
                "provider": provider_id,
                "status": "inconclusive",
                "model": last["model"],
                "latency_ms": last["latency_ms"],
                "evidence": last["evidence"],
                "health_status": provider.state.health_status,
                "success_rate": provider.state.success_rate,
                "checked": checked,
            }
        return {
            **best,
            "health_status": provider.state.health_status,
            "success_rate": provider.state.success_rate,
            "checked": checked,
        }
