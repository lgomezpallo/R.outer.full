import httpx
import pytest
from router import Router, RouteRequest
from router.resolve import resolve_provider, ProviderResolutionError

class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"ok": True}

def test_typo_resolves_to_groq():
    assert resolve_provider("Groc").id == "groq"

def test_unknown_provider_is_explicit():
    with pytest.raises(ProviderResolutionError, match="unknown_provider"):
        resolve_provider("proveedor inventado xyz")

def test_provider_requires_only_name_and_key():
    router = Router(FakeClient(["ok"]))
    router.add_provider("Groq", "secret")
    result = router.route(RouteRequest("hola"))
    assert result.ok and result.provider == "groq"

def test_capability_filtering_rejects_ineligible():
    router = Router(FakeClient(["never"]))
    router.add_provider("Groq", "secret")
    result = router.route(RouteRequest("imagen", required_capabilities=frozenset({"vision"})))
    assert not result.ok
    assert result.error == "no_eligible_provider"

def test_fallback_after_timeout():
    fake = FakeClient([httpx.ReadTimeout("late"), httpx.ReadTimeout("late again"), "second works"])
    router = Router(fake, max_retries=1)
    router.add_provider("Groq", "a")
    router.add_provider("OpenRouter", "b")
    result = router.route(RouteRequest("tarea"))
    assert result.ok
    assert len(result.attempts) == 3
    assert result.attempts[0].provider == "groq"
    assert result.attempts[1].provider == "groq"
    assert result.attempts[2].provider == "openrouter"
    assert result.attempts[2].ok is True

def test_decision_is_auditable():
    router = Router(FakeClient(["ok"]))
    router.add_provider("Groq", "a")
    result = router.route(RouteRequest("tarea", preferred_model_class="strong"))
    assert result.ok
    assert result.decisions
    assert any(
        "preferred_model_class=match" in reason
        for decision in result.decisions
        for reason in decision.reasons
    )

def test_state_affects_ranking_after_failure():
    fake = FakeClient([httpx.ReadTimeout("late"), "fallback ok", "second ok"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    router.add_provider("OpenRouter", "b")
    first = router.route(RouteRequest("tarea"))
    assert first.ok
    assert first.provider == "openrouter"
    second = router.route(RouteRequest("otra"))
    assert second.ok
    assert second.provider == "openrouter"


def test_non_retryable_error_falls_back_immediately():
    fake = FakeClient([RuntimeError("bad payload"), "fallback works"])
    router = Router(fake, max_retries=3)
    router.add_provider("Groq", "a")
    router.add_provider("OpenRouter", "b")
    result = router.route(RouteRequest("tarea"))
    assert result.ok
    assert len(result.attempts) == 2
    assert result.attempts[0].provider == "groq"
    assert result.attempts[1].provider == "groq"
    assert result.attempts[0].model != result.attempts[1].model


def test_retry_then_fallback_sequence():
    fake = FakeClient([httpx.ReadTimeout("1"), httpx.ReadTimeout("2"), "ok"])
    router = Router(fake, max_retries=1)
    router.add_provider("Groq", "a")
    router.add_provider("OpenRouter", "b")
    result = router.route(RouteRequest("x"))
    assert result.ok
    assert [a.provider for a in result.attempts] == ["groq", "groq", "openrouter"]


def test_model_specific_failure_can_try_another_model_same_provider():
    fake = FakeClient([RuntimeError("model rejected request"), "second model works"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    result = router.route(RouteRequest("tarea"))
    assert result.ok
    assert result.provider == "groq"
    assert len(result.attempts) == 2
    assert result.attempts[0].model != result.attempts[1].model


def test_capability_is_verified_only_by_specific_probe():
    fake = FakeClient(["ROUTER_OK"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    result = router.verify_capability("groq", "openai/gpt-oss-20b", "chat")
    assert result["status"] == "verified"
    provider = router.catalog.get("groq")
    model = next(m for m in provider.models if m.id == "openai/gpt-oss-20b")
    assert "chat" in model.verified_capabilities
    assert "json" not in model.verified_capabilities

def test_learned_model_metrics_affect_ranking():
    fake = FakeClient(["ok"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    provider = router.registry.all()[0]
    provider.state.model("llama-3.3-70b-versatile").failure_count = 4
    provider.state.model("openai/gpt-oss-20b").success_count = 4
    result = router.route(RouteRequest("tarea"))
    assert result.ok
    assert result.model == "openai/gpt-oss-20b"


def test_catalog_persists_custom_provider_and_verified_capabilities(tmp_path):
    from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
    path = tmp_path / "catalog.json"
    catalog = ProviderCatalog(storage_path=path)
    catalog.put(ProviderSpec(
        id="custom",
        aliases=("custom ai",),
        base_url="https://example.invalid/v1",
        models=(ModelSpec("model-a", frozenset({"chat","json"}), "standard", 60),),
    ))
    catalog.record_capability("custom", "model-a", "chat", "verified", "test_probe")

    loaded = ProviderCatalog(storage_path=path)
    provider = loaded.get("custom")
    assert provider is not None
    model = provider.models[0]
    assert model.capabilities == frozenset({"chat","json"})
    assert model.verified_capabilities == frozenset({"chat"})


def test_strategic_cost_prefers_cheaper_sufficient_model():
    fake = FakeClient(["ok"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    result = router.route(RouteRequest("simple"))
    assert result.ok
    assert result.model == "openai/gpt-oss-20b"

def test_complex_request_can_raise_strategic_cost_ceiling():
    fake = FakeClient(["ok"])
    router = Router(fake, max_retries=0)
    router.add_provider("Groq", "a")
    result = router.route(RouteRequest(
        "hard",
        preferred_model_class="strong",
        max_strategic_cost=100,
    ))
    assert result.ok
    assert any("strategic_cost=" in reason for reason in result.decisions[0].reasons)


def test_generic_task_conserves_specialist_models():
    from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
    catalog = ProviderCatalog(providers=(
        ProviderSpec(
            id="test",
            aliases=("test",),
            base_url="https://example.invalid/v1",
            strategic_cost=10,
            models=(
                ModelSpec("generic", frozenset({"chat"}), "standard", 50, 10),
                ModelSpec("vision-specialist", frozenset({"chat","vision"}), "standard", 50, 10),
            ),
        ),
    ))
    fake = FakeClient(["ok"])
    router = Router(fake, max_retries=0, catalog=catalog)
    router.add_provider("test", "a", discover=False)
    result = router.route(RouteRequest("hola"))
    assert result.ok
    assert result.model == "generic"
    assert any("specialization_penalty=1" in reason for reason in result.decisions[1].reasons)

def test_inconclusive_capability_ranks_below_unknown():
    from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
    catalog = ProviderCatalog(providers=(
        ProviderSpec(
            id="test",
            aliases=("test",),
            base_url="https://example.invalid/v1",
            models=(
                ModelSpec("unknown", frozenset({"chat"}), "standard", 50, 10),
                ModelSpec("inconclusive", frozenset({"chat"}), "standard", 50, 10),
            ),
        ),
    ))
    catalog.record_capability("test", "inconclusive", "chat", "inconclusive", "probe_timeout")
    fake = FakeClient(["ok"])
    router = Router(fake, max_retries=0, catalog=catalog)
    router.add_provider("test", "a", discover=False)
    result = router.route(RouteRequest("hola"))
    assert result.ok
    assert result.model == "unknown"


def test_discovered_specialists_are_classified_without_becoming_generic_chat():
    from router.catalog import infer_discovered_capabilities
    assert infer_discovered_capabilities("whisper-large-v3") == frozenset({"transcription"})
    assert infer_discovered_capabilities("my-vision-model") == frozenset({"chat", "vision"})
    assert infer_discovered_capabilities("flux-2-dev") == frozenset({"image_generation", "image_editing"})
    assert infer_discovered_capabilities("prompt-guard-2") == frozenset()
