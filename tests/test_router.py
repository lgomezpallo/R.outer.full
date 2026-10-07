import os
import time
import json
import pytest
from router_v1.router import Router, GroqProvider, OpenRouterProvider

# Helper to mock requests
from unittest.mock import patch, MagicMock

# Mock responses for providers

def mock_response(status=200, json_data=None, raise_for_status=None):
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = json_data or {"choices": [{"message": {"content": "mocked"}}]}
    if raise_for_status:
        mock_resp.raise_for_status.side_effect = raise_for_status
    else:
        mock_resp.raise_for_status.return_value = None
    return mock_resp

# Test provider resolution and typo handling

def test_provider_typo_resolution():
    router = Router()
    router.register_provider("groq", api_key="dummy")
    assert "groq" in router.providers
    # typo
    router.register_provider("Groc", api_key="dummy2")
    assert "groq" in router.providers

# Test unknown provider

def test_unknown_provider():
    router = Router()
    with pytest.raises(ValueError, match="unknown_provider"):
        router.register_provider("unknown", api_key="dummy")

# Test ambiguous provider

def test_ambiguous_provider():
    router = Router()
    with pytest.raises(ValueError, match="ambiguous_provider"):
        router.register_provider("open", api_key="dummy")

# Test healthy provider selection

def test_healthy_provider_selection(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    router.register_provider("openrouter", api_key="dummy2")
    # mock health
    router.providers["groq"].health = True
    router.providers["openrouter"].health = True
    router.providers["groq"].latency = 0.1
    router.providers["openrouter"].latency = 0.2
    with patch.object(GroqProvider, "call", return_value={"provider": "groq", "response": {}}):
        resp = router.route("test", {"text": "hi"}, {})
        assert resp["provider"] == "groq"

# Test provider fallback on timeout

def test_fallback_on_timeout(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    router.register_provider("openrouter", api_key="dummy2")
    router.providers["groq"].health = True
    router.providers["openrouter"].health = True
    router.providers["groq"].latency = 0.1
    router.providers["openrouter"].latency = 0.2
    # groq times out
    def groq_call(*args, **kwargs):
        raise requests.Timeout("timeout")
    with patch.object(GroqProvider, "call", groq_call):
        with patch.object(OpenRouterProvider, "call", return_value={"provider": "openrouter", "response": {}}):
            resp = router.route("test", {"text": "hi"}, {})
            assert resp["provider"] == "openrouter"

# Test all providers fail

def test_all_providers_fail(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    router.register_provider("openrouter", api_key="dummy2")
    router.providers["groq"].health = True
    router.providers["openrouter"].health = True
    def fail(*args, **kwargs):
        raise requests.HTTPError("error", response=MagicMock(status_code=500))
    with patch.object(GroqProvider, "call", fail):
        with patch.object(OpenRouterProvider, "call", fail):
            with pytest.raises(RuntimeError, match="all_providers_failed"):
                router.route("test", {"text": "hi"}, {})

# Test rate limit fallback

def test_rate_limit_fallback(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    router.register_provider("openrouter", api_key="dummy2")
    router.providers["groq"].health = True
    router.providers["openrouter"].health = True
    def rate_limit(*args, **kwargs):
        raise requests.HTTPError("rate limit", response=MagicMock(status_code=429))
    with patch.object(GroqProvider, "call", rate_limit):
        with patch.object(OpenRouterProvider, "call", return_value={"provider": "openrouter", "response": {}}):
            resp = router.route("test", {"text": "hi"}, {})
            assert resp["provider"] == "openrouter"

# Test model sufficiency selection (simplified)

def test_model_sufficiency(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    router.register_provider("openrouter", api_key="dummy2")
    router.providers["groq"].health = True
    router.providers["openrouter"].health = True
    router.providers["groq"].latency = 0.1
    router.providers["openrouter"].latency = 0.2
    # groq supports only small model, openrouter supports large
    def groq_call(*args, **kwargs):
        if kwargs["requirements"].get("model") == "llama3-8b":
            raise requests.HTTPError("model not supported", response=MagicMock(status_code=400))
        return {"provider": "groq", "response": {}}
    with patch.object(GroqProvider, "call", groq_call):
        with patch.object(OpenRouterProvider, "call", return_value={"provider": "openrouter", "response": {}}):
            resp = router.route("test", {"text": "hi"}, {"model": "llama3-8b"})
            assert resp["provider"] == "openrouter"

# Test client can change provider without code change

def test_client_change_provider(monkeypatch):
    router = Router()
    router.register_provider("groq", api_key="dummy")
    with patch.object(GroqProvider, "call", return_value={"provider": "groq", "response": {}}):
        resp = router.route("test", {"text": "hi"}, {})
        assert resp["provider"] == "groq"
    # now change to openrouter
    router.register_provider("openrouter", api_key="dummy2")
    with patch.object(OpenRouterProvider, "call", return_value={"provider": "openrouter", "response": {}}):
        resp = router.route("test", {"text": "hi"}, {})
        assert resp["provider"] == "openrouter"
