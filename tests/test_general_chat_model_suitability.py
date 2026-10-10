from types import SimpleNamespace

from router.catalog import ModelSpec, ProviderSpec
from router.selector import _score
from router.types import RouteRequest


def _candidate(model_id):
    model = ModelSpec(
        id=model_id,
        capabilities=frozenset({"chat"}),
        model_class="unknown",
        strategic_cost=10,
    )
    provider = SimpleNamespace(
        spec=ProviderSpec(
            id="test",
            aliases=(),
            base_url="https://example.invalid/v1",
            models=(model,),
            strategic_cost=10,
        ),
        state=SimpleNamespace(
            available=True,
            health_status="healthy",
            models={},
            success_rate=0.0,
            ewma_latency_ms=None,
        ),
    )
    return provider, model


def test_arabic_specialist_not_used_for_spanish_chat():
    provider, model = _candidate("allam-2-7b")
    assert _score(provider, model, RouteRequest(task="Respondé en español")) is None


def test_arabic_specialist_remains_available_for_arabic():
    provider, model = _candidate("allam-2-7b")
    assert _score(provider, model, RouteRequest(task="مرحبا")) is not None


def test_diffusion_architecture_not_selected_for_chat():
    provider, model = _candidate("google/diffusiongemma-26b-a4b-it")
    assert _score(provider, model, RouteRequest(task="Hola")) is None


def test_general_chat_model_is_still_eligible():
    provider, model = _candidate("openai/gpt-oss-20b")
    assert _score(provider, model, RouteRequest(task="Hola")) is not None
