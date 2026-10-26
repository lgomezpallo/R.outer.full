"""Regression: unavailable models cannot dilute specialization scarcity.

All provider data are in-memory fixtures. Billing is simulated locally and
there are no inference, probe, discovery, or account API calls.
"""
from dataclasses import replace
from time import time

import pytest

from router.catalog import BUILTINS, ModelSpec
from router.registry import RegisteredProvider
from router.selector import rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing


def _model(name, *, caps=("chat",), billing="free"):
    return ModelSpec(
        id=name,
        capabilities=frozenset(caps),
        model_class="standard",
        strategic_cost=10,
        billing_status=billing,
    )


def _provider(models):
    spec = next(spec for spec in BUILTINS if spec.id == "groq")
    return RegisteredProvider(
        replace(spec, models=tuple(models)),
        "offline-fixture-not-a-real-credential",
        RuntimeState(),
    )


def _scores(provider, req):
    return {decision.model: decision.score for decision in rank([provider], req)}


def _simulate_local_billing(monkeypatch):
    # Only this pure ranking test substitutes the economic gate. Production
    # remains fail-closed; independent billing tests exercise the real gate.
    import router.selector as selector
    monkeypatch.setattr(
        selector, "billing_ineligibility_reason",
        lambda _provider, model: "billing_paid" if model.billing_status != "free" else None,
    )


def test_paid_models_do_not_dilute_specialist_scarcity(monkeypatch):
    _simulate_local_billing(monkeypatch)
    usable = [_model("vendor/chat"), _model("vendor/vision-usable", caps=("chat", "vision"))]
    req = RouteRequest("ordinary chat")
    baseline = _scores(_provider(usable), req)
    with_paid = _scores(_provider(usable + [
        _model(f"vendor/vision-paid-{i}", caps=("chat", "vision"), billing="paid")
        for i in range(20)
    ]), req)
    assert set(with_paid) == set(baseline)
    assert with_paid == baseline


def test_cooling_models_do_not_dilute_specialist_scarcity(monkeypatch):
    _simulate_local_billing(monkeypatch)
    usable = [_model("vendor/chat"), _model("vendor/vision-usable", caps=("chat", "vision"))]
    req = RouteRequest("ordinary chat")
    baseline = _scores(_provider(usable), req)
    cooling = _provider(usable + [
        _model(f"vendor/vision-cooling-{i}", caps=("chat", "vision"))
        for i in range(20)
    ])
    for model in cooling.spec.models[2:]:
        cooling.state.model(model.id).cooldown_until = time() + 3600
    assert _scores(cooling, req) == baseline


def test_incompatible_models_do_not_dilute_task_specific_scarcity(monkeypatch):
    _simulate_local_billing(monkeypatch)
    usable = [
        _model("vendor/reasoning-general", caps=("chat", "reasoning")),
        _model("vendor/reasoning-vision", caps=("chat", "reasoning", "vision")),
    ]
    req = RouteRequest("complex task", required_capabilities=frozenset({"chat", "reasoning"}))
    baseline = _scores(_provider(usable), req)
    incompatible = [_model(f"vendor/vision-only-{i}", caps=("chat", "vision")) for i in range(20)]
    assert _scores(_provider(usable + incompatible), req) == baseline
