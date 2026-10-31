"""Coverage accounting: cataloged does not mean eligible or actually exercised."""
import pytest

pytestmark = pytest.mark.usefixtures("allow_offline_selector_fixture")
from time import time

from fastapi.testclient import TestClient

from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest


def _model(model_id, capabilities=("chat",), cost=10):
    return ModelSpec(model_id, frozenset(capabilities), "standard", 50, cost)


def test_more_than_one_hundred_models_are_rankable_not_only_four():
    models = tuple(_model(f"chat-{i}") for i in range(125))
    provider = RegisteredProvider(
        ProviderSpec("many", ("many",), "https://example.invalid/v1", models),
        "test-only-key",
        RuntimeState(),
    )
    req = RouteRequest("hola")
    assert len(rank([provider], req)) == 125
    report = model_coverage([provider], req)
    assert report["totals"]["cataloged_models"] == 125
    assert report["totals"]["eligible_models"] == 125
    assert report["totals"]["observed_models"] == 0
    assert report["quality_measured"] is False


def test_exclusion_reasons_match_ranking_and_cooldowns():
    first = RegisteredProvider(
        ProviderSpec(
            "one", ("one",), "https://example.invalid/v1",
            (_model("chat-available"), _model("chat-cooling"),
             _model("embedding-specialist", ()),
             _model("chat-unsupported"), _model("chat-expensive", cost=200)),
            strategic_cost=10,
        ),
        "key", RuntimeState(),
    )
    first.state.model("chat-cooling").cooldown_until = time() + 3600
    first.state.model("chat-unsupported").mark_success(90)
    # An explicitly unsupported capability cannot be ranked even if declared.
    from router.catalog import CapabilityEvidence
    models = list(first.spec.models)
    models[3] = ModelSpec(
        "chat-unsupported", frozenset({"chat"}), "standard", 50, 10,
        (CapabilityEvidence.now("chat", "unsupported", "probe_404"),),
    )
    from dataclasses import replace
    first.spec = replace(first.spec, models=tuple(models))

    second = RegisteredProvider(
        ProviderSpec("two", ("two",), "https://example.invalid/v1",
                     (_model("chat-provider-cooling"),)),
        "key", RuntimeState(),
    )
    second.state.cooldown_until = time() + 3600
    req = RouteRequest("hola", max_strategic_cost=50)
    report = model_coverage([first, second], req)
    assert len(rank([first, second], req)) == report["totals"]["eligible_models"] == 1
    assert report["totals"]["cataloged_models"] == 6
    assert report["totals"]["observed_models"] == 1
    assert report["totals"]["observed_attempts"] == 1
    assert report["providers"][0]["excluded_by_reason"] == {
        "model_cooldown": 1,
        "not_chat_model": 1,
        "unsupported_capability": 1,
        "strategic_cost": 1,
    }
    assert report["providers"][1]["excluded_by_reason"] == {"provider_cooldown": 1}


def test_admin_coverage_endpoint_does_not_call_provider(monkeypatch):
    import router.api as module

    class NoNetworkClient:
        def complete(self, *args, **kwargs):
            raise AssertionError("coverage_must_not_send_requests")

        def discover_models(self, *args, **kwargs):
            raise AssertionError("coverage_must_not_discover")

    catalog = ProviderCatalog(providers=(
        ProviderSpec("audit", ("audit",), "https://example.invalid/v1",
                     (_model("chat-1"), _model("chat-2"))),
    ))
    router = Router(client=NoNetworkClient(), catalog=catalog)
    router.add_provider("audit", "not-a-real-key", discover=False)
    monkeypatch.setattr(module, "router", router)
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "admin-secret")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "iachat-secret")
    client = TestClient(module.app)
    path = "/diagnostics/model-coverage"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer iachat-secret"}).status_code == 401
    response = client.get(path, headers={"Authorization": "Bearer admin-secret"})
    assert response.status_code == 200
    assert response.json()["totals"]["eligible_models"] == 2
    assert "not-a-real-key" not in response.text
    assert "admin-secret" not in response.text
    assert client.get(path + "?capability=unknown", headers={
        "Authorization": "Bearer admin-secret"
    }).status_code == 422
