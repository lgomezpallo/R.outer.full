from __future__ import annotations
import json
import httpx
from .registry import RegisteredProvider
from .types import RouteRequest

class ProviderClient:
    def complete(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        protocol = provider.spec.protocol
        if protocol == "openai-compatible":
            return self._openai_compatible(provider, model, req)
        if protocol == "anthropic":
            return self._anthropic(provider, model, req)
        if protocol == "gemini":
            return self._gemini(provider, model, req)
        raise RuntimeError(f"unsupported_protocol:{protocol}")

    def discover_models(self, provider: RegisteredProvider, timeout_s: float = 12.0) -> list[str]:
        if provider.spec.protocol != "openai-compatible":
            return []
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            response = client.get(
                provider.spec.base_url.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {provider.api_key}", "Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        source = data.get("data") if isinstance(data, dict) else None
        if not isinstance(source, list):
            source = data.get("models") if isinstance(data, dict) else None
        if not isinstance(source, list):
            return []
        result: list[str] = []
        for item in source[:2000]:
            model_id = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
            if isinstance(model_id, str) and model_id.strip() and len(model_id) <= 200:
                result.append(model_id.strip())
        return list(dict.fromkeys(result))

    def _messages(self, req: RouteRequest) -> list[dict]:
        messages: list[dict] = []
        if req.context:
            messages.append({"role": "system", "content": req.context})
        user = req.task
        if req.requirements:
            user += "\n\nRequirements:\n- " + "\n- ".join(req.requirements)
        messages.append({"role": "user", "content": user})
        return messages

    def _openai_compatible(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        payload = {"model": model, "messages": self._messages(req), "temperature": 0.2}
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False) as client:
            response = client.post(
                provider.spec.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("empty_provider_response")
        return text, data

    def _anthropic(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        messages = [{"role": "user", "content": req.task}]
        payload = {"model": model, "messages": messages, "max_tokens": 2048}
        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False) as client:
            response = client.post(provider.spec.base_url.rstrip("/") + "/v1/messages", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        blocks = data.get("content", [])
        text = "\n".join(x.get("text", "") for x in blocks if isinstance(x, dict) and x.get("type") == "text")
        if not text.strip():
            raise RuntimeError("empty_provider_response")
        return text, data

    def _gemini(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        url = provider.spec.base_url.rstrip("/") + f"/models/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": req.task}]}]}
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False) as client:
            response = client.post(url, params={"key": provider.api_key}, json=payload)
            response.raise_for_status()
            data = response.json()
        candidates = data.get("candidates", [])
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text = "\n".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text.strip():
            raise RuntimeError("empty_provider_response")
        return text, data

# Backward compatible name used by existing tests.
OpenAICompatibleClient = ProviderClient
