"""Offline price-gate regressions: account audits are advisory, paid/unknown models blocked."""
from dataclasses import replace
import httpx
import pytest
from router.billing import account_billing_ineligibility_reason
from router.catalog import BUILTINS, ModelSpec, ProviderSpec
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.selector import billing_ineligibility_reason, model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.strict_billing

def _provider(provider_id, status="free"):
    if provider_id == "cloudflare":
        spec = ProviderSpec("cloudflare", ("cloudflare",),
                            "https://api.cloudflare.com/client/v4/accounts/test/ai", models=())
        model_id = "@cf/meta/llama-3.1-8b-instruct"
    else:
        spec = next(item for item in BUILTINS if item.id == provider_id)
        model_id = {"groq": "openai/gpt-oss-20b", "nvidia": "google/gemma-4-31b-it",
                    "openrouter": "vendor/explicit-zero-pricing:free"}[provider_id]
    model = ModelSpec(model_id, frozenset({"chat", "vision"}), "standard", billing_status=status)
    return RegisteredProvider(replace(spec, models=(model,)), "fake-key", RuntimeState())

@pytest.mark.parametrize("provider_id", ["groq", "nvidia", "cloudflare", "openrouter"])
def test_explicitly_free_model_is_eligible_without_account_audit(provider_id):
    provider = _provider(provider_id)
    model = provider.spec.models[0]
    req = RouteRequest("hola", required_capabilities=frozenset({"chat"}), decompose=False)
    assert account_billing_ineligibility_reason(provider_id) is None
    assert billing_ineligibility_reason(provider, model) is None
    assert len(rank([provider], req)) == 1
    assert model_coverage([provider], req)["totals"]["eligible_models"] == 1
    ProviderClient._require_billing_eligible(provider, model.id)

@pytest.mark.parametrize("provider_id", ["groq", "nvidia", "cloudflare", "openrouter"])
@pytest.mark.parametrize("status,reason", [("paid", "billing_paid"), ("unknown", "billing_unverified")])
def test_paid_and_unknown_models_blocked_before_http(monkeypatch, provider_id, status, reason):
    provider = _provider(provider_id, status)
    model = provider.spec.models[0]
    req = RouteRequest("hola", required_capabilities=frozenset({"chat"}), decompose=False)
    assert billing_ineligibility_reason(provider, model) == reason
    assert rank([provider], req) == []
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("unexpected_http"))
    for invoke in (
        lambda: ProviderClient().complete(provider, model.id, req),
        lambda: ProviderClient().probe_capability(provider, model.id, "vision"),
        lambda: ProviderClient()._openai_compatible(provider, model.id, req),
    ):
        with pytest.raises(RuntimeError, match="inference_blocked:" + reason):
            invoke()

def test_selector_mock_cannot_bypass_last_mile_paid_guard(monkeypatch):
    from router import selector
    provider = _provider("openrouter", "paid")
    monkeypatch.setattr(selector, "billing_ineligibility_reason", lambda *_: None)
    monkeypatch.setenv("ROUTER_ALLOW_PAID_MODELS", "1")
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: pytest.fail("unexpected_http"))
    with pytest.raises(RuntimeError, match="inference_blocked:billing_paid"):
        ProviderClient().complete(provider, provider.spec.models[0].id, RouteRequest("hola"))

def test_cloudflare_paid_only_model_stays_blocked_when_marked_free():
    provider = _provider("cloudflare")
    model = replace(provider.spec.models[0], id="@cf/moonshotai/kimi-k2.6")
    provider.spec = replace(provider.spec, models=(model,))
    assert billing_ineligibility_reason(provider, model) == "billing_paid"
    with pytest.raises(RuntimeError, match="inference_blocked:billing_paid"):
        ProviderClient._require_billing_eligible(provider, model.id)

def test_offline_mode_blocks_even_explicitly_free_model(monkeypatch):
    provider = _provider("groq")
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    with pytest.raises(RuntimeError, match="inference_blocked:offline_only"):
        ProviderClient._require_billing_eligible(provider, provider.spec.models[0].id)
