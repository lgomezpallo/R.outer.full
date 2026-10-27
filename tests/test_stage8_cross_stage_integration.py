"""Cross-stage integration contract for stages 1-8, entirely offline.

The default-deny test uses production billing policy. The simulated fallback
tests use pytest's test-only account fixture and an injected client; no real
provider client, discovery endpoint, probe, or inference API is contacted.
"""
from dataclasses import replace
from time import time

import httpx
import pytest

from router.catalog import BUILTINS, ModelSpec, ProviderCatalog
from router.client import ProviderClient
from router.inventory import inventory_report
from router.ranking_diagnostics import ranking_report
from router.router import Router
from router.storage import RouterStore
from router.task_requirements import analyze_task
from router.types import RouteRequest


GROQ_MODEL = "vendor/offline-general"
OPENROUTER_MODEL = "vendor/offline-backup"


def _catalog():
    models = {"groq": GROQ_MODEL, "openrouter": OPENROUTER_MODEL}
    specs = []
    for provider in BUILTINS:
        if provider.id not in models:
            continue
        model = ModelSpec(
            id=models[provider.id],
            capabilities=frozenset({"chat"}),
            model_class="standard",
            priority=50,
            strategic_cost=10,
            billing_status="free",
        )
        specs.append(replace(provider, models=(model,)))
    return ProviderCatalog(tuple(specs))


class NoInferenceClient:
    def __init__(self):
        self.calls = []

    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model))
        raise AssertionError("unexpected_inference_attempt")

    def discover_inventory(self, provider):
        raise AssertionError("unexpected_live_discovery")


@pytest.mark.strict_billing
def test_full_offline_read_path_never_authorizes_unverified_accounts(tmp_path):
    client = NoInferenceClient()
    store = RouterStore(tmp_path / "router.db")
    router = Router(client=client, store=store, catalog=_catalog())
    for name in ("groq", "openrouter"):
        router.add_provider(name, "not-a-real-key", discover=False)

    inventory = inventory_report(router.registry.all())
    assert inventory["totals"]["cataloged"] == 2
    assert inventory["totals"]["reported_free_price"] == 2
    assert inventory["totals"]["eligible"] == 0
    assert inventory["totals"]["blocked"] == 2
    assert inventory["account_billing_verified"] is False

    task = analyze_task("Escribí código en Python")
    assert task["explicit_required_capabilities"] == ["chat"]
    assert task["routing_modified"] is False
    assert "code" in {item["capability"] for item in task["suggested_capabilities"]}

    preview = ranking_report(router.registry.all(), "Escribí código en Python")
    assert preview["cataloged_models"] == 2
    assert preview["ranked_models"] == 0
    assert preview["ranking"] == []
    assert preview["account_billing_verified"] is False

    request = RouteRequest("Escribí código en Python", decompose=False)
    response = router.route(request)
    assert not response.ok
    assert response.error == "no_eligible_provider"
    assert response.attempts == []
    assert client.calls == []
    assert store.model_performance_report() == []
    assert store.provider_routing_report() == []
    assert store.recent_request_traces() == []

    # A direct transport invocation cannot bypass the ranking's denial.
    transport = ProviderClient()
    for provider in router.registry.all():
        with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_account"):
            transport.complete(provider, provider.spec.models[0].id, request)


class OfflineFallbackClient:
    def __init__(self):
        self.calls = []

    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model))
        if provider.spec.id == "groq":
            request = httpx.Request("POST", "https://example.invalid/offline")
            response = httpx.Response(503, request=request, headers={"Retry-After": "3600"})
            raise httpx.HTTPStatusError(
                "simulated model service unavailable", request=request, response=response
            )
        return "offline fallback success", {"offline_fixture": True}


def test_integrated_fallback_metrics_restart_and_ranking_use_same_models(tmp_path):
    # The pytest-only billing fixture permits ranking of explicit free models;
    # the injected client never creates HTTP connections.
    path = tmp_path / "router.db"
    client = OfflineFallbackClient()
    router = Router(
        client=client, store=RouterStore(path), catalog=_catalog(), max_retries=0
    )
    for name in ("groq", "openrouter"):
        router.add_provider(name, "not-a-real-key", discover=False)

    response = router.route(RouteRequest("hello", decompose=False))
    assert response.ok
    assert response.provider == "openrouter"
    assert response.model == OPENROUTER_MODEL
    assert client.calls == [
        ("groq", GROQ_MODEL),
        ("openrouter", OPENROUTER_MODEL),
    ]
    assert [attempt.ok for attempt in response.attempts] == [False, True]

    store = RouterStore(path)
    stats = store.recent_model_stats()
    assert stats[("groq", GROQ_MODEL)]["failure_count"] == 1
    assert stats[("groq", GROQ_MODEL)]["success_count"] == 0
    assert stats[("groq", GROQ_MODEL)]["latency_ms"] is None
    assert stats[("openrouter", OPENROUTER_MODEL)]["success_count"] == 1
    trace = store.recent_request_traces()[0]
    assert trace["fallback"] is True
    assert len(trace["attempts"]) == 2
    aggregates = {row["provider"]: row for row in store.provider_routing_report()}
    assert aggregates["groq"]["failed_attempts"] == 1
    assert aggregates["openrouter"]["successful_requests"] == 1

    restarted = Router(store=RouterStore(path), catalog=_catalog())
    groq = restarted.add_provider("groq", "not-a-real-key", discover=False)
    restarted.add_provider("openrouter", "not-a-real-key", discover=False)
    assert groq.state.available
    assert groq.state.model(GROQ_MODEL).cooldown_until > time() + 3400
    assert groq.state.model(GROQ_MODEL).ewma_latency_ms is None
    preview = ranking_report(restarted.registry.all(), "hello")
    assert preview["ranked_models"] == 1
    assert [(row["provider"], row["model"]) for row in preview["ranking"]] == [
        ("openrouter", OPENROUTER_MODEL)
    ]


class OfflineInventoryClient(NoInferenceClient):
    def __init__(self, ids, prices):
        super().__init__()
        self.ids = ids
        self.prices = prices

    def discover_inventory(self, provider):
        return self.ids, self.prices


@pytest.mark.parametrize(
    ("ids", "prices", "expected_price", "expected_discovered"),
    [
        ([GROQ_MODEL], {GROQ_MODEL: "paid"}, "paid", True),
        ([], {}, "unknown", False),
    ],
)
def test_inventory_refresh_revokes_old_free_claim_before_ranking(
    tmp_path, ids, prices, expected_price, expected_discovered
):
    # Discovery here is a supplied in-memory snapshot, not an API request.
    client = OfflineInventoryClient(ids, prices)
    router = Router(
        client=client,
        catalog=_catalog(),
        store=RouterStore(tmp_path / "router.db"),
    )
    provider = router.add_provider("groq", "not-a-real-key", discover=True)
    model = provider.spec.models[0]
    assert model.billing_status == expected_price
    assert model.discovered_in_last_snapshot is expected_discovered

    report = inventory_report(router.registry.all())
    assert report["totals"]["eligible"] == 0
    assert report["totals"]["blocked"] == 1
    assert ranking_report(router.registry.all(), "hello")["ranked_models"] == 0
    assert router.route(RouteRequest("hello", decompose=False)).attempts == []
    assert client.calls == []
