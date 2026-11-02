"""Stage 6: offline fallback, quota cooldowns, and Retry-After regressions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from time import time

import httpx
import pytest

from router.catalog import BUILTINS, ModelSpec, ProviderCatalog
from router.fallback_policy import MAX_RETRY_AFTER_SECONDS, retry_after_seconds
from router.router import Router
from router.types import RouteRequest


@pytest.mark.parametrize(("value", "expected"), [
    ("3600", 3600.0),
    (" 60 ", 60.0),
    ("0", 0.0),
    ("-5", None),
    ("1.5", None),
    ("not-a-date", None),
    ("", None),
    (None, None),
])
def test_retry_after_delta_seconds_and_invalid_headers(value, expected):
    assert retry_after_seconds(value) == expected


def test_retry_after_http_date_is_timezone_aware_and_deterministic():
    now = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)
    assert retry_after_seconds(format_datetime(now + timedelta(minutes=40)), now=now) == 2400.0
    assert retry_after_seconds(format_datetime(now - timedelta(seconds=1)), now=now) == 0.0


def test_retry_after_absurdly_large_value_is_not_ignored():
    assert retry_after_seconds("9" * 5000) == float(MAX_RETRY_AFTER_SECONDS)


def _http_error(status, retry_after=None):
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("offline simulated response", request=request, response=response)


def test_429_respects_longer_quota_window_and_never_immediately_retries():
    retryable, provider_wide, kind, code, cooldown = Router()._classify_failure(
        _http_error(429, "3600")
    )
    assert (retryable, provider_wide, kind, code) == (False, True, "quota", 429)
    assert cooldown == 3600.0


def test_429_invalid_retry_after_keeps_conservative_default():
    retryable, provider_wide, kind, code, cooldown = Router()._classify_failure(
        _http_error(429, "garbage")
    )
    assert (retryable, provider_wide, kind, code) == (True, True, "quota", 429)
    assert cooldown == 900.0


def test_503_with_retry_after_skips_immediate_retry_of_same_model():
    retryable, provider_wide, kind, code, cooldown = Router()._classify_failure(
        _http_error(503, "600")
    )
    assert (retryable, provider_wide, kind, code) == (False, False, "server", 503)
    assert cooldown == 600.0


def test_503_without_retry_after_retains_previous_retry_policy():
    retryable, provider_wide, kind, code, cooldown = Router()._classify_failure(
        _http_error(503)
    )
    assert (retryable, provider_wide, kind, code) == (True, False, "server", 503)
    assert cooldown == 120.0


class OfflineClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def complete(self, provider, model, req):
        self.calls.append((provider.spec.id, model))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"fixture": True}


def test_503_retry_after_cools_only_failing_model_and_falls_back_offline(monkeypatch):
    spec = next(item for item in BUILTINS if item.id == "groq")
    catalog = ProviderCatalog(providers=(replace(spec, models=(
        ModelSpec("openai/gpt-oss-20b", frozenset({"chat"}), "standard", priority=100, billing_status="free"),
        ModelSpec("llama-3.3-70b-versatile", frozenset({"chat"}), "standard", priority=10, billing_status="free"),
    )),))
    fake = OfflineClient([_http_error(503, "1800"), "fallback response"])
    router = Router(fake, max_retries=3, catalog=catalog)
    router.add_provider("groq", "offline-key", discover=False)
    # Fix candidate order in this policy test: ranking preferences are tested
    # independently and may legitimately prefer the other catalog model.
    from router import router as router_module
    from router.types import Decision
    monkeypatch.setattr(router_module, "rank", lambda *_: [
        Decision("groq", "openai/gpt-oss-20b", 100),
        Decision("groq", "llama-3.3-70b-versatile", 90),
    ])

    result = router.route(RouteRequest("offline test", decompose=False))
    assert result.ok
    assert len(result.attempts) == 2
    assert len(set(fake.calls)) == 2
    assert result.attempts[0].model == "openai/gpt-oss-20b"
    assert result.attempts[1].model == "llama-3.3-70b-versatile"
    provider = router.registry.all()[0]
    assert provider.state.available
    assert provider.state.model("openai/gpt-oss-20b").cooldown_until - time() > 1700
    assert provider.state.model("llama-3.3-70b-versatile").available


def test_429_provider_quota_skips_sibling_models_and_falls_back_to_other_provider(monkeypatch):
    from router import router as router_module
    from router.types import Decision

    groq = next(item for item in BUILTINS if item.id == "groq")
    openrouter = next(item for item in BUILTINS if item.id == "openrouter")
    catalog = ProviderCatalog(providers=(
        replace(groq, models=(
            ModelSpec("openai/gpt-oss-20b", frozenset({"chat"}), "standard", billing_status="free"),
            ModelSpec("llama-3.3-70b-versatile", frozenset({"chat"}), "standard", billing_status="free"),
        )),
        replace(openrouter, models=(
            ModelSpec("vendor/zero-priced-fixture:free", frozenset({"chat"}), "standard", billing_status="free"),
        )),
    ))
    fake = OfflineClient([_http_error(429, "7200"), "other provider ok"])
    router = Router(fake, max_retries=3, catalog=catalog)
    router.add_provider("groq", "offline-key", discover=False)
    router.add_provider("openrouter", "offline-key", discover=False)
    monkeypatch.setattr(router_module, "rank", lambda *_: [
        Decision("groq", "openai/gpt-oss-20b", 100),
        Decision("groq", "llama-3.3-70b-versatile", 90),
        Decision("openrouter", "vendor/zero-priced-fixture:free", 80),
    ])

    result = router.route(RouteRequest("offline test", decompose=False))
    assert result.ok
    assert fake.calls == [
        ("groq", "openai/gpt-oss-20b"),
        ("openrouter", "vendor/zero-priced-fixture:free"),
    ]
    assert router.registry.all()[0].state.cooldown_until - time() > 7000
