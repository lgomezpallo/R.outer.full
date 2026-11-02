"""One real, isolated Router inference using a verified OpenRouter :free model.

Run in any explicitly authorized isolated Python environment with an
OPENROUTER_API_KEY. Never connects to Oracle, changes service configuration,
or enables billing. Does not depend on GitHub Actions or main-branch events.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time

import uvicorn
from pathlib import Path

import httpx

from router.catalog import ProviderCatalog
from router.router import Router

MODEL = "google/gemma-4-26b-a4b-it:free"
MARKER = "ROUTER_REAL_FREE_OK"


def report(status: str, reason: str | None = None) -> None:
    print("LIVE_FREE_STATUS=" + status, flush=True)
    if reason:
        print("LIVE_FREE_REASON=" + reason, flush=True)


def generation_cost(key: str, generation_id: str) -> Decimal | None:
    """Read actual per-generation charge, never issue a second completion."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", generation_id):
        return None
    with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
        for attempt in range(4):
            response = client.get(
                "https://openrouter.ai/api/v1/generation",
                params={"id": generation_id},
                headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            )
            if response.status_code == 404 and attempt < 3:
                time.sleep(2)
                continue
            if response.status_code != 200:
                return None
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or data.get("total_cost") is None:
                return None
            try:
                value = Decimal(str(data["total_cost"]))
            except (InvalidOperation, ValueError, TypeError):
                return None
            return value if value.is_finite() and value >= 0 else None
    return None


def main() -> int:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        report("not_run", "missing_openrouter_key")
        return 2

    # This flag affects only this short-lived process, never router.service.
    os.environ["ROUTER_OFFLINE_ONLY"] = "0"
    print("LIVE_FREE_ENV=isolated_process_online_only", flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="router-first-free-") as work:
            router = Router(
                catalog=ProviderCatalog(storage_path=str(Path(work) / "catalog.json")),
                max_retries=0,
            )
            provider = router.add_provider("OpenRouter", key, discover=True)
            # A fresh successful /models response, not a persisted or mock
            # catalog, must explicitly classify this exact :free ID as free.
            models = [m for m in provider.spec.models if m.id == MODEL]
            if (
                provider.spec.last_inventory_source != "client_discover_inventory"
                or len(models) != 1
                or not models[0].discovered_in_last_snapshot
                or models[0].billing_status != "free"
            ):
                report("not_run", "fresh_zero_price_inventory_unavailable")
                return 2
            provider.spec = router.catalog.put(replace(provider.spec, models=(models[0],)))
            print("LIVE_FREE_PRICING=all_advertised_charges_zero", flush=True)
            print("LIVE_FREE_MODEL=" + MODEL, flush=True)

            # Exercise the actual OpenAI-compatible HTTP endpoint on loopback,
            # not merely Router.route() as the previous smoke did.
            os.environ["ROUTER_DB_FILE"] = str(Path(work) / "router.db")
            os.environ["ROUTER_CATALOG_FILE"] = str(Path(work) / "api-catalog.json")
            os.environ["ROUTER_SERVICE_TOKEN"] = secrets.token_urlsafe(32)
            os.environ["ROUTER_MAX_RETRIES"] = "0"
            for name in (
                "GROQ_API_KEY", "NVIDIA_API_KEY", "MISTRAL_API_KEY",
                "CEREBRAS_API_KEY", "SAMBANOVA_API_KEY",
                "CLOUDFLARE_API_TOKEN",
            ):
                os.environ.pop(name, None)
            for name in (
                "ROUTER_STARTUP_PROVIDER_TESTS", "ROUTER_STARTUP_CAPABILITY_PROFILE",
                "ROUTER_STARTUP_ROUTING_SMOKE", "ROUTER_STARTUP_ORCHESTRATION_SMOKE",
                "ROUTER_STARTUP_HETEROGENEOUS_SMOKE",
            ):
                os.environ[name] = "0"
            from router import api as router_api
            router_api.router = router
            captured = []
            original_process = router.process

            def record_real_result(request):
                result = original_process(request)
                captured.append(result)
                return result

            router.process = record_real_result
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(8)
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                router_api.app, host="127.0.0.1", port=port,
                log_level="error", access_log=False,
            ))
            thread = threading.Thread(
                target=lambda: server.run(sockets=[sock]), daemon=True,
            )
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    if not thread.is_alive():
                        break
                    time.sleep(0.05)
                if not server.started:
                    report("failed", "isolated_http_server_not_started")
                    return 2
                with httpx.Client(timeout=60, trust_env=False) as client:
                    response = client.post(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        headers={"Authorization": "Bearer " + os.environ["ROUTER_SERVICE_TOKEN"]},
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": "Reply with exactly " + MARKER}],
                            "stream": False,
                        },
                    )
                print("LIVE_FREE_HTTP_STATUS=" + str(response.status_code), flush=True)
                if response.status_code != 200:
                    report("failed", "router_http_non_200")
                    return 2
                payload = response.json()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                sock.close()

            if len(captured) != 1:
                report("failed", "unexpected_router_process_count")
                return 2
            result = captured[0]
            answer = payload.get("choices", [{}])[0].get("message", {}).get("content")
            selected = payload.get("router", {})
            if (not result.ok or not result.text or answer != result.text
                    or selected.get("provider") != "openrouter"
                    or selected.get("model") != MODEL
                    or result.provider != "openrouter" or result.model != MODEL):
                report("failed", "router_http_completion_unsuccessful")
                return 2
            print("LIVE_FREE_HTTP_PATH=/v1/chat/completions", flush=True)
            if len(result.attempts) != 1 or not result.attempts[0].ok:
                report("failed", "unexpected_attempt_count")
                return 2
            raw = result.raw if isinstance(result.raw, dict) else {}
            generation_id = raw.get("id")
            if not isinstance(generation_id, str):
                report("cost_unverified", "generation_id_missing")
                return 2
            cost = generation_cost(key, generation_id)
            if cost is None:
                report("cost_unverified", "generation_cost_not_available")
                return 2
            if cost != 0:
                report("failed", "nonzero_generation_cost")
                return 2
            # Only model, response and verified charge; no key or raw headers.
            print("LIVE_FREE_RESPONSE=" + json.dumps(answer[:160], ensure_ascii=True), flush=True)
            print("LIVE_FREE_REPORTED_MODEL=" + str(raw.get("model", "unknown"))[:100], flush=True)
            print("LIVE_FREE_GENERATION_COST_USD=0", flush=True)
            report("success")
            return 0
    except (httpx.HTTPError, ValueError, RuntimeError, OSError, KeyError, TypeError):
        # Never log exception bodies: provider errors may contain request data.
        report("failed", "sanitized_provider_or_runtime_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
