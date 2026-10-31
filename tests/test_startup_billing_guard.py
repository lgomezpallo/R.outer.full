"""Startup diagnostic flags cannot bypass account-level no-overage checks.

All tests are offline: no provider discovery, HTTP clients, or threads run.
"""
from dataclasses import replace

import pytest

from router import api, billing, selector
from router.catalog import BUILTINS, ModelSpec
from router.registry import RegisteredProvider
from router.state import RuntimeState


pytestmark = pytest.mark.strict_billing

STARTUP_FLAGS = (
    "ROUTER_STARTUP_PROVIDER_TESTS",
    "ROUTER_STARTUP_CAPABILITY_PROFILE",
    "ROUTER_STARTUP_ROUTING_SMOKE",
    "ROUTER_STARTUP_ORCHESTRATION_SMOKE",
    "ROUTER_STARTUP_HETEROGENEOUS_SMOKE",
)
CREDENTIAL_ENV = (
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "NVIDIA_API_KEY",
    "MISTRAL_API_KEY",
    "CEREBRAS_API_KEY",
    "SAMBANOVA_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_WORKERS_AI_BASE_URL",
)


def _offline_provider(*, status="unknown"):
    spec = next(item for item in BUILTINS if item.id == "groq")
    model = ModelSpec(
        "offline-model", frozenset({"chat"}), "standard", billing_status=status
    )
    return RegisteredProvider(
        replace(spec, models=(model,)), "not-a-real-key", RuntimeState()
    )


def _isolate_startup(monkeypatch, *, status="unknown"):
    for name in (*STARTUP_FLAGS, *CREDENTIAL_ENV):
        monkeypatch.delenv(name, raising=False)
    provider = _offline_provider(status=status)
    monkeypatch.setattr(api.router, "load_persisted_providers", lambda: 0)
    monkeypatch.setattr(api.router.registry, "all", lambda: [provider])
    return provider


@pytest.mark.parametrize("flag", STARTUP_FLAGS)
def test_startup_never_runs_active_checks_without_account_evidence(monkeypatch, flag, capsys):
    _isolate_startup(monkeypatch)
    monkeypatch.setenv(flag, "1")

    def forbidden(*args, **kwargs):
        pytest.fail("startup scheduled or executed an inference-capable check")

    monkeypatch.setattr(api.router, "test_provider", forbidden)
    monkeypatch.setattr(api.threading, "Thread", forbidden)
    monkeypatch.setattr(api.router, "route", forbidden)
    monkeypatch.setattr(api.router, "process", forbidden)

    api.load_environment_providers()

    assert "ROUTER_STARTUP_ACTIVE_CHECKS_SKIPPED" in capsys.readouterr().out


def test_all_startup_flags_and_spoofed_selector_still_deny(monkeypatch):
    _isolate_startup(monkeypatch)
    for flag in STARTUP_FLAGS:
        monkeypatch.setenv(flag, "1")
    # A forged selector cannot override the independent account-level check.
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *a: None)
    monkeypatch.setattr(
        api.threading, "Thread",
        lambda *a, **kw: pytest.fail("background inference thread created"),
    )
    monkeypatch.setattr(
        api.router, "test_provider",
        lambda *a, **kw: pytest.fail("provider inference test called"),
    )
    assert billing.account_billing_ineligibility_reason("groq") is None
    api.load_environment_providers()


def test_startup_still_does_not_launch_when_no_flags_are_set(monkeypatch):
    _isolate_startup(monkeypatch)
    monkeypatch.setattr(
        api, "_startup_has_eligible_inference",
        lambda: pytest.fail("account check should be lazy when no flags enabled"),
    )
    api.load_environment_providers()


def test_explicitly_free_model_schedules_startup_check_with_mocked_thread(monkeypatch):
    _isolate_startup(monkeypatch, status="free")
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _id: None)
    monkeypatch.setenv("ROUTER_STARTUP_ROUTING_SMOKE", "1")
    started = []

    class FakeThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            started.append(("created", target.__name__))

        def start(self):
            started.append(("started", None))

    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    api.load_environment_providers()
    assert started == [
        ("created", "_startup_routing_smoke"),
        ("started", None),
    ]
