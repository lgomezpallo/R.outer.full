"""Offline regressions: image inputs must not fall back to text-only models.

All provider calls are synthetic. No HTTP inference, probes, or billing access.
"""
from router import Router, RouteRequest
from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.selector import model_coverage, rank


IMAGE = "data:image/png;base64,iVBORw0KGgo="  # Synthetic only; never sent to a provider.


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model, req))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"synthetic": True}


def _router(client, *, vision_models=2):
    models = [
        ModelSpec("text-only", frozenset({"chat"}), "standard", 100, 5, billing_status="free"),
    ]
    for i in range(vision_models):
        models.append(ModelSpec(
            f"vision-model-{i}", frozenset({"chat", "vision"}), "standard",
            90 - i * 10, 5, billing_status="free",
        ))
    catalog = ProviderCatalog(providers=(
        ProviderSpec(
            id="groq", aliases=("groq",),
            base_url="https://api.groq.com/openai/v1",
            models=tuple(models), strategic_cost=10,
        ),
    ))
    router = Router(client, catalog=catalog, max_retries=0)
    router.add_provider("groq", "synthetic-token", discover=False)
    return router


def test_image_request_hard_filters_text_only_even_without_explicit_vision():
    client = FakeClient(["vision-ok"])
    router = _router(client)
    req = RouteRequest("Describe the photo", images=(IMAGE,), decompose=False)
    ranked = rank(router.registry.all(), req)
    assert ranked
    assert all(item.model.startswith("vision-model") for item in ranked)
    coverage = model_coverage(router.registry.all(), req)
    assert coverage["required_capabilities"] == ["chat", "vision"]
    assert coverage["totals"]["eligible_models"] == 2
    assert coverage["providers"][0]["excluded_by_reason"]["missing_capability"] == 1
    response = router.route(req)
    assert response.ok
    assert response.model.startswith("vision-model")
    assert client.calls[0][2].required_capabilities == frozenset({"chat", "vision"})
    assert client.calls[0][2].images == (IMAGE,)


def test_image_fallback_keeps_vision_and_original_attachment():
    client = FakeClient([RuntimeError("synthetic vision failure"), "backup-ok"])
    router = _router(client)
    result = router.route(RouteRequest("Read image", images=(IMAGE,)))
    assert result.ok
    assert len(client.calls) == 2
    assert client.calls[0][1] != client.calls[1][1]
    assert all(model.startswith("vision-model") for _, model, _ in client.calls)
    assert all("vision" in req.required_capabilities and req.images == (IMAGE,)
               for _, _, req in client.calls)


def test_image_with_no_vision_model_fails_closed_without_dispatch():
    client = FakeClient(["should-never-run"])
    router = _router(client, vision_models=0)
    result = router.route(RouteRequest("What is in this image?", images=(IMAGE,)))
    assert not result.ok
    assert result.error == "no_eligible_provider"
    assert client.calls == []


def test_image_cannot_be_lost_by_explicit_decomposition():
    client = FakeClient(["vision-direct"])
    router = _router(client)
    response = router.process(RouteRequest(
        "Analyze and summarize the photo", images=(IMAGE,), decompose=True,
    ))
    assert response.ok
    assert len(client.calls) == 1
    assert client.calls[0][2].images == (IMAGE,)
    assert "vision" in client.calls[0][2].required_capabilities
    assert response.attempts[0].phase == "direct"
