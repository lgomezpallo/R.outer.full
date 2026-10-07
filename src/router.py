import time
import json
import re
from typing import List, Dict, Any, Optional

# --- Proveedores conocidos y sus configuraciones ---
PROVIDERS = {
    "groq": {
        "canonical": "groq",
        "endpoint": "https://api.groq.com/openai/v1",
        "models": {"llama3-8b": {"capacity": 8}},
        "aliases": ["groq", "groc"],
    },
    "openrouter": {
        "canonical": "openrouter",
        "endpoint": "https://openrouter.ai/api/v1",
        "models": {"gpt-4o-mini": {"capacity": 4}},
        "aliases": ["openrouter", "openrouter.ai"],
    },
}

# --- Helper de similitud simple ---
def _similar(a: str, b: str) -> bool:
    return a.lower() in b.lower() or b.lower() in a.lower()

# --- Resolución de nombre ---
class ProviderResolution:
    def __init__(self, name: str):
        self.name = name
        self.canonical = None
        self.status = "unknown_provider"
        self._resolve()

    def _resolve(self):
        # 1. Coincidencia exacta
        for key, cfg in PROVIDERS.items():
            if self.name.lower() == key:
                self.canonical = key
                self.status = "resolved"
                return
        # 2. Alias
        for key, cfg in PROVIDERS.items():
            if self.name.lower() in [a.lower() for a in cfg["aliases"]]:
                self.canonical = key
                self.status = "resolved"
                return
        # 3. Coincidencia aproximada
        matches = [k for k in PROVIDERS if _similar(self.name, k)]
        if len(matches) == 1:
            self.canonical = matches[0]
            self.status = "resolved"
        elif len(matches) > 1:
            self.status = "ambiguous_provider"
        else:
            self.status = "unknown_provider"

# --- Estado dinámico ---
class ProviderState:
    def __init__(self, canonical: str, api_key: str):
        self.canonical = canonical
        self.api_key = api_key
        self.last_success = None
        self.last_failure = None
        self.latency = None
        self.cooldown_until = None
        self.health = True

    def record_success(self, latency: float):
        self.last_success = time.time()
        self.latency = latency

    def record_failure(self, error: str):
        self.last_failure = time.time()
        self.cooldown_until = time.time() + 30  # 30s cooldown

# --- Router principal ---
class Router:
    def __init__(self, providers: List[Dict[str, str]]):
        # providers: list of {name, api_key}
        self.states: Dict[str, ProviderState] = {}
        for p in providers:
            res = ProviderResolution(p["name"])
            if res.status != "resolved":
                raise ValueError(f"Provider {p['name']} unresolved: {res.status}")
            self.states[res.canonical] = ProviderState(res.canonical, p["api_key"])

    def _select_provider(self, task: Dict[str, Any]) -> Optional[ProviderState]:
        # Simplified selection: pick first healthy provider
        for state in self.states.values():
            if state.health and (state.cooldown_until is None or state.cooldown_until < time.time()):
                return state
        return None

    def _call_provider(self, state: ProviderState, task: Dict[str, Any]) -> Dict[str, Any]:
        # Mocked call: simulate latency and possible failure
        start = time.time()
        try:
            # Simulate network latency
            time.sleep(0.05)
            # Simulate random failure for demo
            if state.canonical == "groq" and task.get("simulate_fail"):
                raise Exception("Simulated failure")
            response = {"provider": state.canonical, "content": f"Response from {state.canonical}"}
            latency = time.time() - start
            state.record_success(latency)
            return response
        except Exception as e:
            latency = time.time() - start
            state.record_failure(str(e))
            raise

    def route(self, task: Dict[str, Any]) -> Dict[str, Any]:
        provider = self._select_provider(task)
        if not provider:
            raise RuntimeError("No available provider")
        try:
            return self._call_provider(provider, task)
        except Exception as e:
            # Fallback to next provider
            for state in self.states.values():
                if state == provider:
                    continue
                try:
                    return self._call_provider(state, task)
                except Exception:
                    continue
            raise RuntimeError("All providers failed")

# --- Cliente ficticio ---
if __name__ == "__main__":
    router = Router([{"name": "Groq", "api_key": "sk-xxxx"}, {"name": "OpenRouter", "api_key": "sk-yyyy"}])
    print(router.route({"task": "Hello"}))
