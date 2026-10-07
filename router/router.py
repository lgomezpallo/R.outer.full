from __future__ import annotations
from time import perf_counter
import httpx
from .client import OpenAICompatibleClient
from .registry import ProviderRegistry
from .selector import rank
from .types import Attempt, ProviderCredential, RouteRequest, RouteResponse

class Router:
    def __init__(self, client: OpenAICompatibleClient | None = None) -> None:
        self.registry = ProviderRegistry()
        self.client = client or OpenAICompatibleClient()

    def add_provider(self, name: str, api_key: str) -> None:
        self.registry.add(ProviderCredential(name, api_key))

    def route(self, req: RouteRequest) -> RouteResponse:
        decisions = rank(self.registry.all(), req)
        if not decisions:
            return RouteResponse(False, None, None, None, [], [], "no_eligible_provider")
        attempts: list[Attempt] = []
        by_id = {p.spec.id: p for p in self.registry.all()}
        for decision in decisions:
            provider = by_id[decision.provider]
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
                cooldown = 30.0 if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429 else 10.0
                provider.state.mark_failure(error, cooldown)
                attempts.append(Attempt(decision.provider, decision.model, False, latency, error))
        return RouteResponse(False, None, None, None, attempts, decisions, "all_attempts_failed")
