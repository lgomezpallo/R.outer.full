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
    assert any("preferred_model_class=match" in reason for reason in result.decisions[0].reasons)

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
    assert result.attempts[1].provider == "openrouter"
