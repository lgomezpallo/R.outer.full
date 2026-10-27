"""Stage 6: offline fallback must not dispatch stale ranked candidates.

No provider inference, probes or external requests. All account approvals below
are monkeypatched inside synthetic tests, never enabled in production.
"""
from dataclasses import replace
from importlib import import_module
from time import time

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, CapabilityEvidence, ModelSpec, ProviderCatalog
from router.router import Router
from router.types import Decision, RouteRequest

pytestmark = pytest.mark.strict_billing
routing_module = import_module("router.router")

MODELS = (
    ModelSpec("openai/gpt-oss-20b", frozenset({"chat"}), "standard", billing_status="free"),
    ModelSpec("llama-3.3-70b-versatile", frozenset({"chat"}), "standard", billing_status="free"),
)


def _router(client, *, retries=0):
    groq = next(spec for spec in BUILTINS if spec.id == "groq")
    router = Router(client=client, max_retries=retries, catalog=ProviderCatalog(
        (replace(groq, models=MODELS),)
    ))
    router.add_provider("groq", "synthetic-key", discover=False)
    return router


def _rank(monkeypatch):
    monkeypatch.setattr(routing_module, "rank", lambda *_: [
        Decision("groq", model.id, 100 - index) for index, model in enumerate(MODELS)
    ])


def test_fallback_skips_model_whose_capability_was_revoked(monkeypatch):
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda *_: None)
    calls = []

    class Client:
        def complete(self, provider, model, req):
            calls.append(model)
            provider.spec = replace(provider.spec, models=(
                provider.spec.models[0],
                replace(provider.spec.models[1], capabilities=frozenset(),
                        evidence=()),
            ))
            raise RuntimeError("synthetic_failure")

    router = _router(Client())
    _rank(monkeypatch)
    result = router.route(RouteRequest("offline", decompose=False))
    assert not result.ok
    assert len(result.attempts) == 1
    assert calls == [MODELS[0].id]


def test_fallback_skips_model_that_entered_cooldown_after_ranking(monkeypatch):
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda *_: None)
    calls = []

    class Client:
        def complete(self, provider, model, req):
            calls.append(model)
            provider.state.model(MODELS[1].id).cooldown_until = time() + 600
            raise RuntimeError("synthetic_failure")

    router = _router(Client())
    _rank(monkeypatch)
    result = router.route(RouteRequest("offline", decompose=False))
    assert not result.ok
    assert len(result.attempts) == 1
    assert calls == [MODELS[0].id]


def test_retry_stops_when_required_capability_becomes_unsupported(monkeypatch):
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda *_: None)
    calls = []

    class Client:
        def complete(self, provider, model, req):
            calls.append(model)
            provider.spec = replace(provider.spec, models=(
                replace(provider.spec.models[0], evidence=(
                    CapabilityEvidence("chat", "unsupported", "offline-fixture", "2026-10-27T00:00:00Z"),
                )),
                provider.spec.models[1],
            ))
            raise httpx.ReadTimeout("synthetic_timeout")

    router = _router(Client(), retries=2)
    monkeypatch.setattr(routing_module, "rank", lambda *_: [
        Decision("groq", MODELS[0].id, 100)
    ])
    result = router.route(RouteRequest("offline", decompose=False))
    assert not result.ok
    assert len(result.attempts) == 1
    assert calls == [MODELS[0].id]
