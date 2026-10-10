"""Regression tests for model-first routing. No network or paid API calls."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.router import Router
from router.types import RouteRequest

def provider(name, model, *, hits=0, misses=0, latency=None, caps=("chat",)):
    spec = ProviderSpec(id=name, aliases=(name,), base_url="https://example.com",
                        models=(ModelSpec(id=model, capabilities=frozenset(caps), model_class="standard"),))
    state = RuntimeState()
    item = state.model(model)
    item.success_count, item.failure_count = hits, misses
    item.ewma_latency_ms = latency
    return RegisteredProvider(spec, "fake", state)

def main():
    good = provider("groq", "alpha", hits=12, latency=250)
    bad = provider("cloudflare", "beta", misses=10, latency=200)
    unknown = provider("openrouter", "gamma")
    req = RouteRequest(task="Hola")
    order = rank([bad, unknown, good], req)
    assert order[0].provider == "groq", order
    assert order[-1].provider == "cloudflare", order
    assert "observed_samples=0" in rank([unknown], req)[0].reasons
    special = provider("nvidia", "llava-test", caps=("chat", "vision"))
    regular = provider("groq", "alpha")
    assert rank([special, regular], req)[0].provider == "groq"
    assert rank([special, regular], RouteRequest(task="Imagen",required_capabilities=frozenset({"vision"})))[0].provider == "nvidia"

    class Dummy:
        def __init__(self):
            self.calls = []
        def complete(self, provider, model, req):
            self.calls.append(provider.spec.id)
            if provider.spec.id == "groq":
                raise ValueError("model failed")
            return "ok", {}
    client = Dummy()
    r = Router(client=client, catalog=ProviderCatalog(providers=(good.spec, unknown.spec, bad.spec)), max_retries=0)
    r.registry._providers = {p.spec.id:p for p in (good, unknown, bad)}
    result = r.route(req)
    assert result.ok and result.provider == "openrouter", result
    assert client.calls == ["groq", "openrouter"], client.calls
    print("OK: reliability, neutral untested, specialization, capabilities, global fallback; zero API calls")

if __name__ == "__main__":
    main()
