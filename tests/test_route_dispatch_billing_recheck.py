"""Offline regression: routing decisions are not permission to bill.

The router rechecks billing immediately before dispatch, on retries, and
between fallback candidates, even when a custom client bypasses ProviderClient.
"""
from dataclasses import replace
from importlib import import_module

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS, ModelSpec, ProviderCatalog
from router.router import Router
from router.types import Decision, RouteRequest

pytestmark = pytest.mark.strict_billing
routing_module = import_module("router.router")


def _router(client, *, include_openrouter=False, retries=0):
    groq = next(spec for spec in BUILTINS if spec.id == "groq")
    groq = replace(
        groq,
        models=(ModelSpec("openai/gpt-oss-20b", frozenset({"chat"}), "standard", billing_status="free"),),
    )
    specs = [groq]
    if include_openrouter:
        openrouter = next(spec for spec in BUILTINS if spec.id == "openrouter")
        openrouter = replace(
            openrouter,
            models=(ModelSpec("vendor/zero-price", frozenset({"chat"}), "standard", billing_status="free"),),
        )
        specs.append(openrouter)
    router = Router(client=client, max_retries=retries, catalog=ProviderCatalog(tuple(specs)))
    router.add_provider("groq", "offline-key", discover=False)
    if include_openrouter:
        router.add_provider("openrouter", "offline-key", discover=False)
    return router


class RecordingClient:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model))
        if self.callback:
            return self.callback(provider, model, req)
        raise AssertionError("custom_client_must_not_run")


def _decision(provider, model):
    return Decision(provider, model, 100, ["forged_offline_ranking"])


def test_forged_ranking_cannot_call_custom_client_without_account_evidence(monkeypatch):
    client = RecordingClient()
    router = _router(client, include_openrouter=True)
    monkeypatch.setattr(routing_module, "rank", lambda providers, req: [
        _decision("groq", "openai/gpt-oss-20b"),
        _decision("openrouter", "vendor/zero-price"),
        _decision("unregistered", "fake"),
        _decision("groq", "not-in-catalog"),
    ])
    result = router.route(RouteRequest("hola", decompose=False))
    assert not result.ok
    assert result.error == "no_eligible_provider"
    assert result.attempts == []
    assert client.calls == []


def test_fallback_rechecks_changed_model_billing_before_custom_client(monkeypatch):
    # Simulated account approval is test-only; no real provider is contacted.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda provider_id: None)
    holder = {}

    def fail_first(provider, model, req):
        next_provider = next(p for p in holder["router"].registry.all() if p.spec.id == "openrouter")
        next_provider.spec = replace(next_provider.spec, models=(
            replace(next_provider.spec.models[0], billing_status="paid"),
        ))
        raise RuntimeError("simulated_model_failure")

    client = RecordingClient(fail_first)
    router = _router(client, include_openrouter=True)
    holder["router"] = router
    monkeypatch.setattr(routing_module, "rank", lambda providers, req: [
        _decision("groq", "openai/gpt-oss-20b"),
        _decision("openrouter", "vendor/zero-price"),
    ])
    result = router.route(RouteRequest("hola", decompose=False))
    assert not result.ok
    assert result.error == "all_attempts_failed"
    assert len(result.attempts) == 1
    assert client.calls == [("groq", "openai/gpt-oss-20b")]


def test_retry_stops_when_account_evidence_is_revoked(monkeypatch):
    allowed = {"value": True}
    monkeypatch.setattr(
        billing, "account_billing_ineligibility_reason",
        lambda provider_id: None if allowed["value"] else "billing_unverified_account",
    )

    def fail_and_revoke(provider, model, req):
        allowed["value"] = False
        raise httpx.ReadTimeout("offline_timeout")

    client = RecordingClient(fail_and_revoke)
    router = _router(client, retries=2)
    monkeypatch.setattr(routing_module, "rank", lambda providers, req: [
        _decision("groq", "openai/gpt-oss-20b"),
    ])
    result = router.route(RouteRequest("hola", decompose=False))
    assert not result.ok
    assert result.error == "all_attempts_failed"
    assert len(result.attempts) == 1
    assert client.calls == [("groq", "openai/gpt-oss-20b")]
