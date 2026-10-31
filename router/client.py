from __future__ import annotations
import json
import httpx
from .billing import CLOUDFLARE_PAID_ONLY, offline_only
from .modality_contract import completion_transport_error
from .registry import RegisteredProvider
from .types import RouteRequest


def _is_cloudflare_workers_ai(base_url: str) -> bool:
    return base_url.startswith("https://api.cloudflare.com/client/v4/accounts/") and base_url.rstrip("/").endswith("/ai")


class ProviderClient:
    @staticmethod
    def _require_billing_eligible(provider: RegisteredProvider, model: str) -> None:
        """Last-mile guard: never send inference for an unapproved model.

        Covers direct client calls and probes, not just Router's ranking.
        Free-model eligibility does not guarantee account-level billing outcomes.
        """
        if offline_only():
            raise RuntimeError("inference_blocked:offline_only")
        from .billing import billing_endpoint_ineligibility_reason
        from .selector import billing_ineligibility_reason

        # Independent transport guard: even an alternate ranking implementation
        # or a mocked selector cannot authorize a foreign billing destination.
        endpoint_block = billing_endpoint_ineligibility_reason(
            provider.spec.id, provider.spec.base_url, provider.spec.protocol
        )
        if endpoint_block is not None:
            raise RuntimeError(f"inference_blocked:{endpoint_block}")

        matches = [item for item in provider.spec.models if item.id == model]
        if not matches:
            raise RuntimeError("inference_blocked:billing_unverified_model")
        # Independently reject ambiguous persisted catalogs, even if a custom
        # selector or ranking implementation incorrectly picks the free row.
        if len(matches) != 1:
            raise RuntimeError("inference_blocked:billing_ambiguous_model")
        model_spec = matches[0]
        # Independently enforce the per-model price even if the selector is replaced.
        if model_spec.billing_status != "free":
            reason = "billing_paid" if model_spec.billing_status == "paid" else "billing_unverified"
            raise RuntimeError(f"inference_blocked:{reason}")
        if provider.spec.id == "cloudflare" and model in CLOUDFLARE_PAID_ONLY:
            raise RuntimeError("inference_blocked:billing_paid")
        reason = billing_ineligibility_reason(provider, model_spec)
        if reason is not None:
            raise RuntimeError(f"inference_blocked:{reason}")


    def complete(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        # Independent last-mile check: do not post specialist jobs to /chat/completions.
        unsupported = completion_transport_error(req)
        if unsupported is not None:
            raise RuntimeError(unsupported)
        self._require_billing_eligible(provider, model)
        protocol = provider.spec.protocol
        if protocol == "openai-compatible":
            return self._openai_compatible(provider, model, req)
        if protocol == "anthropic":
            return self._anthropic(provider, model, req)
        if protocol == "gemini":
            return self._gemini(provider, model, req)
        raise RuntimeError(f"unsupported_protocol:{protocol}")


    def discover_models(self, provider: RegisteredProvider, timeout_s: float = 12.0) -> list[str]:
        """Compatibility interface: list every discovered model, including paid ones."""
        ids, _ = self.discover_inventory(provider, timeout_s)
        return ids


    def discover_inventory(
        self, provider: RegisteredProvider, timeout_s: float = 12.0
    ) -> tuple[list[str], dict[str, str]]:
        """Inventory plus explicit OpenRouter billing classification (no inference of free)."""
        if offline_only():
            raise RuntimeError("inventory_blocked:offline_only")
        if provider.spec.protocol != "openai-compatible":
            return [], {}
        # Discovery sends the configured API key too. Check its destination
        # independently of routing so catalog refresh cannot leak credentials
        # to an arbitrary URL supplied by a mutable provider configuration.
        from .billing import billing_endpoint_ineligibility_reason
        endpoint_block = billing_endpoint_ineligibility_reason(
            provider.spec.id, provider.spec.base_url, provider.spec.protocol
        )
        if endpoint_block is not None:
            raise RuntimeError(f"inventory_blocked:{endpoint_block}")
        base = provider.spec.base_url.rstrip("/")
        if _is_cloudflare_workers_ai(base):
            # Inventory is not billing authorization. Include even known paid
            # models for honest diagnostics, but never infer a free account plan.
            result: list[str] = []
            billing: dict[str, str] = {}
            with httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False) as client:
                for page in range(1, 51):
                    response = client.get(
                        base + f"/models/search?hide_experimental=true&include_deprecated=false&per_page=100&page={page}",
                        headers={"Authorization": f"Bearer {provider.api_key}", "Accept": "application/json"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("success") is False:
                        raise ValueError("cloudflare_inventory_invalid_response")
                    source = payload.get("result")
                    if not isinstance(source, list):
                        raise ValueError("cloudflare_inventory_invalid_result")
                    for item in source:
                        model_id = item.get("name") if isinstance(item, dict) else None
                        if isinstance(model_id, str) and model_id.startswith("@cf/") and len(model_id) <= 200:
                            result.append(model_id)
                            billing[model_id] = "paid" if model_id in CLOUDFLARE_PAID_ONLY else "unknown"
                    if len(source) < 100:
                        break
                else:
                    # Never persist a partial inventory as a complete snapshot.
                    raise RuntimeError("cloudflare_inventory_pagination_limit")
            return list(dict.fromkeys(result)), billing

        with httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False) as client:
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
            return [], {}
        result: list[str] = []
        billing: dict[str, str] = {}
        is_openrouter = base == "https://openrouter.ai/api/v1"
        # NVIDIA /models is inventory, not proof of free billing.
        # Routing authorization is enforced independently in selector.py.
        # Never silently truncate a provider inventory: a duplicate paid row
        # beyond the old 2,000-item slice could be ignored after a free row,
        # incorrectly classifying that model as free. Process the whole bounded
        # snapshot or reject it without returning partial billing evidence.
        if len(source) > 10_000:
            raise ValueError("provider_inventory_too_large")
        for item in source:
            model_id = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
                continue
            model_id = model_id.strip()
            if is_openrouter:
                # A price is free only if both prompt and completion prices
                # are explicitly numeric zero. Missing or invalid prices are unknown.
                pricing = item.get("pricing") if isinstance(item, dict) and isinstance(item.get("pricing"), dict) else {}
                prompt_price = pricing.get("prompt")
                completion_price = pricing.get("completion")
                status = "unknown"
                # Fail closed when OpenRouter adds an unfamiliar pricing field:
                # even zero token prices cannot authorize an unexamined fee.
                known_pricing_fields = {
                    "prompt", "completion", "request", "image", "web_search",
                    "internal_reasoning", "input_cache_read", "input_cache_write",
                }
                try:
                    if set(pricing).issubset(known_pricing_fields) and prompt_price is not None and completion_price is not None:
                        from decimal import Decimal, InvalidOperation
                        prompt = Decimal(str(prompt_price))
                        completion = Decimal(str(completion_price))
                        if prompt.is_finite() and completion.is_finite() and prompt >= 0 and completion >= 0:
                            # Request, image and reasoning surcharges also matter:
                            # a zero token price alone does not mean a free request.
                            charges = []
                            for key in ("request", "image", "web_search", "internal_reasoning", "input_cache_read", "input_cache_write"):
                                # An explicitly present null is not a zero
                                # price: it is an unverified potential charge.
                                # Missing fields mean no advertised surcharge,
                                # but present fields require numeric evidence.
                                if key in pricing:
                                    if pricing[key] is None:
                                        raise InvalidOperation
                                    charge = Decimal(str(pricing[key]))
                                    if not charge.is_finite() or charge < 0:
                                        raise InvalidOperation
                                    charges.append(charge)
                            status = "free" if prompt == 0 and completion == 0 and all(c == 0 for c in charges) else "paid"
                except (InvalidOperation, ValueError, TypeError):
                    pass
                # A provider inventory can contain duplicate IDs with conflicting
                # pricing records. Never let a later free row overwrite an
                # earlier paid/unknown row (or vice versa). The most restrictive
                # observation wins regardless of API ordering.
                severity = {"free": 0, "unknown": 1, "paid": 2}
                previous = billing.get(model_id)
                if previous is None or severity[status] > severity[previous]:
                    billing[model_id] = status
            result.append(model_id)
        return list(dict.fromkeys(result)), billing


    def probe_capability(self, provider: RegisteredProvider, model: str, capability: str) -> dict:
        # Non-chat probes issue HTTP requests directly; enforce before any POST.
        self._require_billing_eligible(provider, model)
        base = provider.spec.base_url.rstrip("/")
        if capability in {"chat", "json"}:
            task = "Reply with exactly ROUTER_OK" if capability == "chat" else 'Return exactly {"router_ok":true}'
            text, _ = self.complete(
                provider,
                model,
                RouteRequest(task=task, required_capabilities=frozenset({"chat"}), decompose=False, timeout_s=20),
            )
            if capability == "chat":
                return {"status": "verified" if text.strip() == "ROUTER_OK" else "inconclusive", "evidence": "active_text_probe"}
            try:
                ok = bool(json.loads(text).get("router_ok") is True)
            except Exception:
                ok = False
            return {"status": "verified" if ok else "unsupported", "evidence": "active_json_probe"}

        if capability in {"reasoning", "code", "coding", "summarization", "document"}:
            if capability == "reasoning":
                task = "Solve 17*19. Reply with only the number."
                validator = lambda text: text.strip() == "323"
                evidence = "active_reasoning_probe"
            elif capability in {"code", "coding"}:
                task = "Write a Python function named add(a, b) that returns a+b. Return only code."
                validator = lambda text: "def add" in text and "return" in text and ("a + b" in text or "a+b" in text)
                evidence = "active_code_probe"
            elif capability == "summarization":
                task = "Summarize in at most 6 words: The red car stopped at the traffic light because it had turned red."
                validator = lambda text: 1 <= len(text.strip().split()) <= 8 and "red" in text.lower()
                evidence = "active_summarization_probe"
            else:
                task = "Document: ALPHA=73; BETA=19. Question: what is ALPHA? Reply with only the value."
                validator = lambda text: text.strip() == "73"
                evidence = "active_document_probe"
            text, _ = self.complete(
                provider,
                model,
                RouteRequest(task=task, required_capabilities=frozenset({"chat"}), decompose=False, timeout_s=25),
            )
            ok = bool(validator(text))
            return {"status": "verified" if ok else "unsupported", "evidence": evidence}

        if provider.spec.protocol != "openai-compatible":
            return {"status": "inconclusive", "evidence": "probe_not_implemented_for_protocol"}

        headers = {"Authorization": f"Bearer {provider.api_key}"}
        try:
            if capability == "tools":
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Call the get_probe_value tool with key router_ok. Do not answer normally."}],
                    "tools": [{
                        "type": "function",
                        "function": {
                            "name": "get_probe_value",
                            "description": "Return a probe value for a key.",
                            "parameters": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                                "required": ["key"],
                                "additionalProperties": False,
                            },
                        },
                    }],
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_tokens": 128,
                }
                path = "/v1/chat/completions" if _is_cloudflare_workers_ai(base) else "/chat/completions"
                with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(base + path, headers={**headers, "Content-Type": "application/json"}, json=payload)
                if not (200 <= response.status_code < 300):
                    return self._probe_http_result(response, "active_tools_probe")
                try:
                    data = response.json()
                    message = data["choices"][0]["message"]
                    calls = message.get("tool_calls") or []
                    ok = bool(calls and calls[0].get("function", {}).get("name") == "get_probe_value")
                except Exception:
                    ok = False
                return {
                    "status": "verified" if ok else "unsupported",
                    "evidence": "active_tools_probe",
                    "http_status": response.status_code,
                }

            if capability == "long_context":
                marker = "ROUTER_LONG_CONTEXT_84721"
                filler = ("alpha beta gamma delta epsilon zeta eta theta " * 1100)
                task = filler + "\nIMPORTANT MARKER: " + marker + "\nReply with exactly the marker and nothing else."
                text, _ = self.complete(
                    provider,
                    model,
                    RouteRequest(task=task, required_capabilities=frozenset({"chat"}), decompose=False, timeout_s=45),
                )
                return {
                    "status": "verified" if text.strip() == marker else "unsupported",
                    "evidence": "active_long_context_probe_approx_10k_tokens",
                }

            if capability == "vision":
                pixel = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
                image_url = (
                    "https://assets.ngc.nvidia.com/products/api-catalog/phi-3-5-vision/example1a.jpg"
                    if base == "https://integrate.api.nvidia.com/v1"
                    else "data:image/png;base64," + pixel
                )
                content = [
                    {"type": "text", "text": "Describe the image in one word."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
                payload = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 12}
                path = "/v1/chat/completions" if _is_cloudflare_workers_ai(base) else "/chat/completions"
                with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                    response = client.post(base + path, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_vision_probe")

            if capability == "speech":
                if _is_cloudflare_workers_ai(base):
                    url = base + "/run/" + model
                    spanish = model.lower().endswith("aura-2-es")
                    payload = {
                        "text": "Hola",
                        "speaker": "aquila" if spanish else "asteria",
                        "encoding": "mp3",
                    }
                else:
                    url = base + "/audio/speech"
                    lower_model = model.lower()
                    if "orpheus-arabic" in lower_model:
                        voice = "fahad"
                    elif "orpheus" in lower_model:
                        voice = "troy"
                    else:
                        voice = "alloy"
                    payload = {"model": model, "input": "Hello", "voice": voice, "response_format": "wav"}
                with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
                    response = client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_speech_probe")

            if capability == "transcription":
                wav = self._silence_wav()
                if _is_cloudflare_workers_ai(base):
                    import base64
                    payload = {
                        "audio": base64.b64encode(wav).decode("ascii"),
                        "task": "transcribe",
                    }
                    with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
                        response = client.post(
                            base + "/run/" + model,
                            headers={**headers, "Content-Type": "application/json"},
                            json=payload,
                        )
                else:
                    files = {"file": ("probe.wav", wav, "audio/wav")}
                    data = {"model": model, "response_format": "json"}
                    with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
                        response = client.post(base + "/audio/transcriptions", headers=headers, files=files, data=data)
                return self._probe_http_result(response, "active_transcription_probe")

            if capability == "image_generation":
                if _is_cloudflare_workers_ai(base):
                    url = base + "/run/" + model
                    prompt = "A red circle on a white background"
                    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                        if any(token in model.lower() for token in ("flux-2-dev", "flux-2-klein")):
                            response = client.post(
                                url,
                                headers=headers,
                                files={"prompt": (None, prompt)},
                            )
                        else:
                            response = client.post(
                                url,
                                headers={**headers, "Content-Type": "application/json"},
                                json={"prompt": prompt},
                            )
                else:
                    url = base + "/images/generations"
                    payload = {"model": model, "prompt": "A red circle on a white background", "n": 1}
                    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                        response = client.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload)
                return self._probe_http_result(response, "active_image_generation_probe")

            if capability == "image_editing":
                import base64
                pixel = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=")
                prompt = "Change the image color while preserving the simple composition"
                if _is_cloudflare_workers_ai(base):
                    url = base + "/run/" + model
                    files = {
                        "prompt": (None, prompt),
                        "input_image_0": ("probe.png", pixel, "image/png"),
                    }
                    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                        response = client.post(url, headers=headers, files=files)
                else:
                    url = base + "/images/edits"
                    files = {"image": ("probe.png", pixel, "image/png")}
                    data = {"model": model, "prompt": prompt}
                    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
                        response = client.post(url, headers=headers, files=files, data=data)
                return self._probe_http_result(response, "active_image_editing_probe")

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
        messages.extend({"role": role, "content": content} for role, content in req.conversation if role in ("user", "assistant"))
        user = req.task
        if req.requirements:
            user += "\n\nRequirements:\n- " + "\n- ".join(req.requirements)
        if req.images:
            content = [{"type": "text", "text": user}]
            content.extend({"type": "image_url", "image_url": {"url": source}} for source in req.images)
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user})
        return messages


    def _openai_compatible(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        self._require_billing_eligible(provider, model)
        payload = {"model": model, "messages": self._messages(req), "temperature": 0.2}
        base = provider.spec.base_url.rstrip("/")
        chat_path = "/v1/chat/completions" if _is_cloudflare_workers_ai(base) else "/chat/completions"
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False, trust_env=False) as client:
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
        self._require_billing_eligible(provider, model)
        if req.images:
            content = [{"type": "text", "text": req.task}]
            content.extend({
                "type": "image",
                "source": {"type": "base64", "media_type": source.split(";", 1)[0][5:],
                           "data": source.split(",", 1)[1]},
            } for source in req.images)
            messages = [{"role": "user", "content": content}]
        else:
            messages = [{"role": "user", "content": req.task}]
        payload = {"model": model, "messages": messages, "max_tokens": 2048}
        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False, trust_env=False) as client:
            response = client.post(provider.spec.base_url.rstrip("/") + "/v1/messages", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        blocks = data.get("content", [])
        text = "\n".join(x.get("text", "") for x in blocks if isinstance(x, dict) and x.get("type") == "text")
        if not text.strip():
            raise RuntimeError("empty_provider_response")
        return text, data


    def _gemini(self, provider: RegisteredProvider, model: str, req: RouteRequest) -> tuple[str, dict]:
        self._require_billing_eligible(provider, model)
        url = provider.spec.base_url.rstrip("/") + f"/models/{model}:generateContent"
        parts = [{"text": req.task}]
        if req.images:
            parts.extend({
                "inline_data": {"mime_type": source.split(";", 1)[0][5:],
                                "data": source.split(",", 1)[1]}
            } for source in req.images)
        payload = {"contents": [{"parts": parts}]}
        with httpx.Client(timeout=req.timeout_s, follow_redirects=False, trust_env=False) as client:
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
