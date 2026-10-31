"""Offline proof that provider HTTP never inherits proxy or CA environment settings.

HTTPX defaults to trust_env=True, which can forward bearer credentials and
inference requests through an unexpected HTTPS_PROXY/ALL_PROXY. The tests
replace HTTP entirely; they never contact model providers.
"""
import ast
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from router import billing
from router.catalog import BUILTINS
from router.client import ProviderClient
from router.registry import RegisteredProvider
from router.state import RuntimeState
from router.types import RouteRequest


pytestmark = pytest.mark.strict_billing


def test_every_provider_http_constructor_disables_environment_trust():
    source = Path(__file__).resolve().parents[1] / "router" / "client.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    constructors = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "Client"
    ]
    assert constructors, "no provider HTTP constructors found"
    for node in constructors:
        kwargs = {item.arg: item.value for item in node.keywords}
        assert isinstance(kwargs.get("trust_env"), ast.Constant), f"line {node.lineno}"
        assert kwargs["trust_env"].value is False, f"line {node.lineno}"
        assert isinstance(kwargs.get("follow_redirects"), ast.Constant), f"line {node.lineno}"
        assert kwargs["follow_redirects"].value is False, f"line {node.lineno}"


@pytest.mark.parametrize("operation", ["inventory", "completion", "capability_probe"])
def test_provider_http_ignores_proxy_environment_with_mock_transport(monkeypatch, operation):
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://untrusted.proxy.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/does/not/exist/ca.pem")
    # Simulated only in this strict-billing test; production remains default-deny.
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda _: None)

    spec = next(item for item in BUILTINS if item.id == "groq")
    # Simulate zero model price as well as account approval for this HTTP-only test.
    spec = replace(spec, models=(replace(spec.models[0], billing_status="free"),))
    provider = RegisteredProvider(spec, "offline-fake-token", RuntimeState())
    model = spec.models[0].id
    constructions = []

    class MockHTTP:
        def __init__(self, **kwargs):
            constructions.append(kwargs)
            assert kwargs.get("trust_env") is False
            assert kwargs.get("follow_redirects") is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            assert url == "https://api.groq.com/openai/v1/models"
            return httpx.Response(
                200, request=httpx.Request("GET", url),
                json={"data": [{"id": model}]},
            )

        def post(self, url, **kwargs):
            assert url == "https://api.groq.com/openai/v1/chat/completions"
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "ROUTER_OK"}}]},
            )

    monkeypatch.setattr(httpx, "Client", MockHTTP)
    client = ProviderClient()
    if operation == "inventory":
        models, _ = client.discover_inventory(provider)
        assert model in models
    elif operation == "completion":
        result, _ = client.complete(provider, model, RouteRequest("hello"))
        assert result == "ROUTER_OK"
    else:
        result = client.probe_capability(provider, model, "vision")
        assert result["status"] == "verified"
    assert len(constructions) == 1
