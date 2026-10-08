from __future__ import annotations
import json
import httpx
from .registry import RegisteredProvider
from .types import RouteRequest

CLOUDFLARE_PAID_ONLY = frozenset({
    "@cf/moonshotai/kimi-k2.6",
    "@cf/moonshotai/kimi-k2.7-code",
    "@cf/zai-org/glm-5.2",
    "@cf/zai-org/glm-5.3",
    "@cf/zai-org/glm-5.3-flash",
    "@cf/deepseek-ai/deepseek-v4-flash-0731",
    "@cf/deepseek-ai/deepseek-v4-pro-0813",
})

def _is_cloudflare_workers_ai(base_url: str) -> bool:
    return base_url.startswith("https://api.cloudflare.com/client/v4/accounts/") and base_url.rstrip("/").endswith("/ai")

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
        base = provider.spec.base_url.rstrip("/")
        if _is_cloudflare_workers_ai(base):
            result: list[str] = []
            with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
                for page in range(1, 6):
                    response = client.get(
                        base + f"/models/search?hide_experimental=true&include_deprecated=false&per_page=100&page={page}",
                        headers={"Authorization": f"Bearer {provider.api_key}", "Accept": "application/json"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    source = payload.get("result", []) if isinstance(payload, dict) else []
                    if not isinstance(source, list):
                        break
                    for item in source:
                        model_id = item.get("name") if isinstance(item, dict) else None
                        if (
                            isinstance(model_id, str)
                            and model_id.startswith("@cf/")
                            and model_id not in CLOUDFLARE_PAID_ONLY
                        ):
                            result.append(model_id)
                    if len(source) < 100:
                        break
            return list(dict.fromkeys(result))

        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            response = client.get(
                base + "/models",
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
        is_openrouter = base == "https://openrouter.ai/api/v1"
        for item in source[:2000]:
            model_id = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
                continue
            model_id = model_id.strip()
            if is_openrouter:
                pricing = item.get("pricing") if isinstance(item, dict) and isinstance(item.get("pricing"), dict) else {}
                prompt_price = pricing.get("prompt")
                completion_price = pricing.get("completion")
                zero_priced = False
                try:
                    zero_priced = (
                        prompt_price is not None
                        and completion_price is not None
                        and float(prompt_price) == 0.0
                        and float(completion_price) == 0.0
                    )
                except (TypeError, ValueError):
                    zero_priced = False
                if not (model_id.endswith(":free") or model_id == "openrouter/free" or zero_priced):
                    continue
            result.append(model_id)
        return list(dict.fromkeys(result))

    def probe_capability(self, provider: RegisteredProvider, model: str, capability: str) -> dict:
        base = provider.spec.base_url.rstrip("/")
        if capability in {"chat", "json"}:
            task = "Reply with exactly ROUTER_OK" if capability == "chat" else 'Return exactly {"router_ok":true}'
            text, _ = self.complete(
                provider,
                model,
                RouteRequest(task=task, required_capabilities=frozenset({"chat"}), decompose=False, timeout_s=20),
            )
            if capability == "chat":
                return {"status": "verified" if text.strip() == "ROUTER_OK" else "unsupported", "evidence": "active_text_probe"}
            try:
                ok = bool(json.loads(text).get("router_ok") is True)
            except Exception:
                ok = False
            return {"status": "verified" if ok else "unsupported", "evidence": "active_json_probe"}

        if provider.spec.protocol != "openai-compatible":
            return {"status": "inconclusive", "evidence": "probe_not_implemented_for_protocol"}

        headers = {"Authorization": f"Bearer {provider.api_key}"}
        try:
            if capability == "vision":
                pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
                content = [
                    {"type": "text", "text": "Describe the image in one word."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + pixel}},
                ]
                payload = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 12}
                path = "/v1/chat/completions" if _is_cloudflare_workers_ai(base) else "/chat/completions"
                with httpx.Client(timeout=30, follow_redirects=False) as client:
                    response = client.post(base + path, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_vision_probe")

            if capability == "speech":
                if _is_cloudflare_workers_ai(base):
                    url = base + "/run/" + model
                    payload = {"text": "Hello"}
                else:
                    url = base + "/audio/speech"
                    payload = {"model": model, "input": "Hello", "voice": "alloy", "response_format": "wav"}
                with httpx.Client(timeout=45, follow_redirects=False) as client:
                    response = client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_speech_probe")

            if capability == "transcription":
                if _is_cloudflare_workers_ai(base):
                    return {"status": "inconclusive", "evidence": "cloudflare_transcription_requires_native_format"}
                wav = self._silence_wav()
                files = {"file": ("probe.wav", wav, "audio/wav")}
                data = {"model": model, "response_format": "json"}
                with httpx.Client(timeout=45, follow_redirects=False) as client:
                    response = client.post(base + "/audio/transcriptions", headers=headers, files=files, data=data)
                return self._probe_http_result(response, "active_transcription_probe")

            if capability == "image_generation":
                if _is_cloudflare_workers_ai(base):
                    url = base + "/run/" + model
                    payload = {"prompt": "A red circle on a white background"}
                else:
                    url = base + "/images/generations"
                    payload = {"model": model, "prompt": "A red circle on a white background", "n": 1}
                with httpx.Client(timeout=60, follow_redirects=False) as client:
                    response = client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_image_generation_probe")

            return {"status": "inconclusive", "evidence": "no_specific_probe_defined"}
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                return self._probe_http_result(exc.response, "active_probe_http_error")
            return {"status": "inconclusive", "evidence": f"probe_network_error:{type(exc).__name__}"}

    def _probe_http_result(self, response: httpx.Response, evidence: str) -> dict:
        if 200 <= response.status_code < 300:
            status = "verified"
        elif response.status_code in {404, 405, 415}:
            status = "unsupported"
        else:
            status = "inconclusive"
        return {"status": status, "evidence": evidence, "http_status": response.status_code}

    def _silence_wav(self) -> bytes:
        import struct
        sample_rate = 8000
        samples = 2000
        data = b"\x00\x00" * samples
        header = (
            b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data))
        )
        return header + data

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
        base = provider.spec.base_url.rstrip("/")
        chat_path = "/v1/chat/completions" if _is_cloudflare_workers_ai(base) else "/chat/completions"
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False) as client:
            response = client.post(
                base + chat_path,
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
