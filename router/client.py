from __future__ import annotations
import httpx
from .registry import RegisteredProvider
from .types import RouteRequest

class OpenAICompatibleClient:
    def complete(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        messages = []
        if req.context:
            messages.append({"role": "system", "content": req.context})
        user = req.task
        if req.requirements:
            user += "\n\nRequirements:\n- " + "\n- ".join(req.requirements)
        messages.append({"role": "user", "content": user})
        payload = {"model": model, "messages": messages, "temperature": 0.2}
        with httpx.Client(timeout=req.timeout_s) as client:
            response = client.post(
                provider.spec.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("empty_provider_response")
        return text, data
