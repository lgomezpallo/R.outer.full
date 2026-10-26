"""Strict offline regression: provider catalog prices do not prove account no-overage status.

Unlike historical selector/ranking fixtures, these tests exercise production's
default-deny account gate without any simulated account authorization.
"""
from dataclasses import replace

import httpx
import pytest

from router.billing import account_billing_ineligibility_reason
from router.catalog import BUILTINS, ModelSpec, ProviderCatalog, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.router import Router
from router.selector import billing_ineligibility_reason, model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest


pytestmark = pytest.mark.strict_billing


def _provider(provider_id: str) -> RegisteredProvider:
    if provider_id == "cloudflare":
        spec = ProviderSpec(
            id="cloudflare",
            aliases=("cloudflare",),
            base_url="https://api.cloudflare.com/client/v4/accounts/test/ai",
            models=(),
        )
        model_id = "@cf/meta/llama-3.1-8b-instruct"
    else:
        spec = next(item for item in BUILTINS if item.id == provider_id)
        model_id = {
            "groq": "openai/gpt-oss-20b",
            "nvidia": "google/gemma-4-31b-it",
            "openrouter": "vendor/explicit-zero-pricing",
        }[provider_id]
    # Even an explicitly free catalog entry cannot establish the account plan.
    model = ModelSpec(model_id, frozenset({"chat", "vision"}), "standard", billing_status="free")
    return RegisteredProvider(replace(spec, models=(model,)), "offline-fake-key", RuntimeState())


@pytest.mark.parametrize("provider_id", ["groq", "nvidia", "cloudflare", "openrouter"])
def test_all_four_providers_default_deny_even_if_model_is_marked_free(monkeypatch, provider_id):
    provider = _provider(provider_id)
    model = provider.spec.models[0]
    request = RouteRequest("hola", required_capabilities=frozenset({"chat"}), decompose=False)
    assert account_billing_ineligibility_reason(provider_id) == "billing_unverified_account"
    assert billing_ineligibility_reason(provider, model) is not None
    assert rank([provider], request) == []
    coverage = model_coverage([provider], request)
    assert coverage["totals"]["eligible_models"] == 0
    assert coverage["totals"]["excluded_models"] == 1

    def forbid_network(*args, **kwargs):
        pytest.fail("HTTP client created before account billing authorization")

    monkeypatch.setattr(httpx, "Client", forbid_network)
    client = ProviderClient()
    for invoke in (
        lambda: client.complete(provider, model.id, request),
        lambda: client.probe_capability(provider, model.id, "vision"),
        lambda: client._openai_compatible(provider, model.id, request),
    ):
        with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_account"):
            invoke()


@pytest.mark.parametrize("provider_id", ["groq", "nvidia", "cloudflare", "openrouter"])
def test_router_provider_test_audit_and_route_do_not_infer(monkeypatch, provider_id):
    provider = _provider(provider_id)
    model = provider.spec.models[0]
    router = Router(catalog=ProviderCatalog((provider.spec,)), client=ProviderClient())
    router.add_provider(provider_id, provider.api_key, discover=False)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    assert router.route(RouteRequest("hola", decompose=False)).error == "no_eligible_provider"
    test = router.test_provider(provider_id)
    assert test["status"] == "inconclusive"
    assert test["checked"] == []
    probe = router.verify_capability(provider_id, model.id, "chat")
    assert probe["status"] == "inconclusive"
    assert probe["evidence"].startswith("probe_blocked:billing_")
    audit = router.audit_provider_capabilities(provider_id)
    assert audit["verified"] == 0
    assert audit["inconclusive"] >= 1


def test_account_gate_cannot_be_bypassed_by_mocking_selector_or_env(monkeypatch):
    from router import selector
    provider = _provider("openrouter")
    model = provider.spec.models[0]
    monkeypatch.setenv("ROUTER_BILLING_VERIFIED", "1")
    monkeypatch.setenv("ROUTER_ALLOW_PAID_MODELS", "1")
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *args: None)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("unexpected HTTP"))
    with pytest.raises(RuntimeError, match="inference_blocked:billing_unverified_account"):
        ProviderClient().complete(provider, model.id, RouteRequest("hola"))


def test_explicitly_paid_groq_model_is_rejected_even_before_account_gate():
    provider = _provider("groq")
    paid = replace(provider.spec.models[0], billing_status="paid")
    assert billing_ineligibility_reason(provider, paid) == "billing_paid"
