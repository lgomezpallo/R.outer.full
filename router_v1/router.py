import time
import json
import logging
from typing import Dict, Any, List, Optional
import requests
from Levenshtein import distance as levenshtein_distance

logger = logging.getLogger("router_v1")
logging.basicConfig(level=logging.INFO)

# ---------------------------
# Provider abstractions
# ---------------------------
class Provider:
    """Base class for all providers."""

    def __init__(self, name: str, api_key: str):
        self.name = name
        self.api_key = api_key
        self.endpoint = ""
        self.model = ""
        self.headers = {}
        self.health = True
        self.latency = 0.0
        self.cooldown_until = 0.0
        self.success_rate = 1.0
        self.last_error = None
        self.last_response_time = None

    def health_check(self) -> bool:
        """Return True if provider is healthy."""
        return self.health

    def call(self, task: str, context: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def is_available(self) -> bool:
        return self.health_check() and time.time() > self.cooldown_until

    def record_success(self, latency: float):
        self.latency = latency
        self.success_rate = min(1.0, self.success_rate + 0.01)
        self.last_error = None

    def record_failure(self, error: Exception, latency: float):
        self.latency = latency
        self.success_rate = max(0.0, self.success_rate - 0.05)
        self.last_error = str(error)
        # simple cooldown on 5xx or rate limit
        if isinstance(error, requests.HTTPError) and error.response.status_code >= 500:
            self.cooldown_until = time.time() + 30

# ---------------------------
# Concrete providers
# ---------------------------
class GroqProvider(Provider):
    def __init__(self, api_key: str):
        super().__init__("groq", api_key)
        self.endpoint = "https://api.groq.com/openai/v1"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def call(self, task: str, context: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "model": requirements.get("model", "llama3-8b"),
            "messages": [
                {"role": "system", "content": f"You are a helpful assistant for task {task}"},
                {"role": "user", "content": context.get("text", "")}
            ],
            "temperature": requirements.get("temperature", 0.7)
        }
        start = time.time()
        try:
            resp = requests.post(f"{self.endpoint}/chat/completions", headers=self.headers, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            latency = time.time() - start
            self.record_success(latency)
            return {"provider": self.name, "response": data}
        except Exception as e:
            latency = time.time() - start
            self.record_failure(e, latency)
            raise

class OpenRouterProvider(Provider):
    def __init__(self, api_key: str):
        super().__init__("openrouter", api_key)
        self.endpoint = "https://openrouter.ai/api/v1"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def call(self, task: str, context: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "model": requirements.get("model", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": f"You are a helpful assistant for task {task}"},
                {"role": "user", "content": context.get("text", "")}
            ],
            "temperature": requirements.get("temperature", 0.7)
        }
        start = time.time()
        try:
            resp = requests.post(f"{self.endpoint}/chat/completions", headers=self.headers, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            latency = time.time() - start
            self.record_success(latency)
            return {"provider": self.name, "response": data}
        except Exception as e:
            latency = time.time() - start
            self.record_failure(e, latency)
            raise

# ---------------------------
# Router implementation
# ---------------------------
class Router:
    def __init__(self):
        self.providers: Dict[str, Provider] = {}
        self.provider_aliases = {
            "groq": "groq",
            "openrouter": "openrouter",
            "open router": "openrouter",
            "openrouter.ai": "openrouter"
        }

    def register_provider(self, name: str, api_key: str):
        canonical = self._resolve_name(name)
        if canonical == "groq":
            self.providers[canonical] = GroqProvider(api_key)
        elif canonical == "openrouter":
            self.providers[canonical] = OpenRouterProvider(api_key)
        else:
            raise ValueError(f"Unknown provider {name}")

    def _resolve_name(self, name: str) -> str:
        name_lc = name.lower()
        if name_lc in self.providers:
            return name_lc
        if name_lc in self.provider_aliases:
            return self.provider_aliases[name_lc]
        # approximate match
        candidates = []
        for known in self.provider_aliases.values():
            if levenshtein_distance(name_lc, known) <= 2:
                candidates.append(known)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError("ambiguous_provider")
        raise ValueError("unknown_provider")

    def _select_provider(self, requirements: Dict[str, Any]) -> Provider:
        # simple selection: healthy, lowest latency, highest success_rate
        candidates = [p for p in self.providers.values() if p.is_available()]
        if not candidates:
            raise RuntimeError("no_available_providers")
        # sort by latency then success_rate
        candidates.sort(key=lambda p: (p.latency, -p.success_rate))
        return candidates[0]

    def route(self, task: str, context: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        tried = set()
        while True:
            provider = self._select_provider(requirements)
            if provider.name in tried:
                raise RuntimeError("all_providers_failed")
            tried.add(provider.name)
            try:
                result = provider.call(task, context, requirements)
                # audit log
                logger.info(f"Provider {provider.name} succeeded in {provider.latency:.2f}s")
                return result
            except requests.HTTPError as e:
                if e.response.status_code in {429, 500, 502, 503, 504}:
                    logger.warning(f"Provider {provider.name} failed with {e.response.status_code}, retrying fallback")
                    continue
                else:
                    raise
            except Exception as e:
                logger.warning(f"Provider {provider.name} error: {e}")
                continue

# ---------------------------
# Example client
# ---------------------------
if __name__ == "__main__":
    router = Router()
    router.register_provider("groq", api_key="<YOUR_GROQ_KEY>")
    router.register_provider("openrouter", api_key="<YOUR_OPENROUTER_KEY>")
    resp = router.route(
        task="translate",
        context={"text": "Hello world"},
        requirements={"model": "llama3-8b", "temperature": 0.7}
    )
    print(json.dumps(resp, indent=2))
