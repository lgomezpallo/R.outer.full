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
        error_type = "provider_unavailable" if retryable else "provider_error"
        error_code: int | None = None
        cooldown = 10.0 if provider_wide else 0.0
        if isinstance(exc, httpx.TimeoutException):
            error_type = "timeout"
            error_code = 408
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            error_code = status
            retryable = status in {408, 429, 500, 502, 503, 504}
            provider_wide = status in {401, 403, 408, 429, 500, 502, 503, 504}
            if status == 429:
                error_type, cooldown = "rate_limit", 30.0
            elif status in {408, 504}:
                error_type, cooldown = "timeout", 10.0
            elif status >= 500:
                error_type, cooldown = "provider_unavailable", 10.0
            elif status in {401, 403}:
                error_type, cooldown = "authentication", 120.0
            else:
                error_type, cooldown = "provider_error", 0.0
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
                    provider.state.mark_failure(decision.model, error, cooldown)
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

        probes = {
            "chat": ("Reply with exactly ROUTER_OK", lambda text: text.strip() == "ROUTER_OK"),
            "json": ('Return exactly this JSON object: {"router_ok":true}', lambda text: json.loads(text).get("router_ok") is True),
        }
        if capability not in probes:
            updated = self.catalog.record_capability(provider_id, model_id, capability, "inconclusive", "no_specific_probe_defined")
            provider.spec = updated
            return {"provider": provider_id, "model": model_id, "capability": capability, "status": "inconclusive"}

        prompt, validator = probes[capability]
        req = RouteRequest(
            task=prompt,
            required_capabilities=frozenset({"chat"}),
            decompose=False,
            application_name="router-capability-probe",
            timeout_s=20,
        )
        started = perf_counter()
        try:
            text, _ = self.client.complete(provider, model_id, req)
            ok = bool(validator(text))
            status = "verified" if ok else "unsupported"
            evidence = "specific_probe_passed" if ok else "specific_probe_failed"
        except Exception as exc:
            status = "inconclusive"
            evidence = f"probe_error:{type(exc).__name__}"
        latency = int((perf_counter() - started) * 1000)
        updated = self.catalog.record_capability(provider_id, model_id, capability, status, evidence)
        provider.spec = updated
        return {
            "provider": provider_id,
            "model": model_id,
            "capability": capability,
            "status": status,
            "latency_ms": latency,
            "evidence": evidence,
        }
