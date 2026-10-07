from __future__ import annotations
from time import perf_counter
import httpx
from .catalog import ProviderCatalog
from .client import OpenAICompatibleClient
from .registry import ProviderRegistry
from .selector import rank
from .types import Attempt, ProviderCredential, RouteRequest, RouteResponse

class Router:
    def __init__(self, client: OpenAICompatibleClient | None = None, max_retries: int = 1, catalog: ProviderCatalog | None = None) -> None:
        self.registry = ProviderRegistry(catalog)
        self.catalog = self.registry.catalog
        self.client = client or OpenAICompatibleClient()
        self.max_retries = max(0, max_retries)

    def add_provider(self, name: str, api_key: str) -> None:
        self.registry.add(ProviderCredential(name, api_key))

    def route(self, req: RouteRequest) -> RouteResponse:
        decisions = rank(self.registry.all(), req)
        if not decisions:
            return RouteResponse(False, None, None, None, [], [], "no_eligible_provider")
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
                    provider.state.mark_success(latency)
                    attempts.append(Attempt(decision.provider, decision.model, True, latency))
                    return RouteResponse(True, text, decision.provider, decision.model, attempts, decisions, raw=raw)
                except (httpx.TimeoutException, httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError, ValueError) as exc:
                    latency = int((perf_counter() - started) * 1000)
                    error = f"{type(exc).__name__}: {exc}"
                    attempts.append(Attempt(decision.provider, decision.model, False, latency, error))
                    retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
                    if isinstance(exc, httpx.HTTPStatusError):
                        retryable = exc.response.status_code in {408, 429, 500, 502, 503, 504}
                    if retryable and attempt_index < self.max_retries:
                        continue
                    provider_wide = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
                    cooldown = 0.0
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                        provider_wide = status in {401, 403, 408, 429, 500, 502, 503, 504}
                        cooldown = 30.0 if status == 429 else (10.0 if provider_wide else 0.0)
                    elif provider_wide:
                        cooldown = 10.0
                    provider.state.mark_failure(error, cooldown)
                    if provider_wide:
                        blocked_providers.add(decision.provider)
                    break
        return RouteResponse(False, None, None, None, attempts, decisions, "all_attempts_failed")
