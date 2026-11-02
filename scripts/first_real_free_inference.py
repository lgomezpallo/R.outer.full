"""One real, isolated Router inference using a verified OpenRouter :free model.

This script runs only in the GitHub Actions main-branch smoke step. It never
connects to Oracle, changes service configuration, or enables billing.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from router.catalog import ProviderCatalog
from router.router import Router
from router.types import RouteRequest

MODEL = "google/gemma-4-26b-a4b-it:free"
MARKER = "ROUTER_REAL_FREE_OK"
REPO = "lgomezpallo/R.outer.full"


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
    if (
        os.getenv("GITHUB_ACTIONS") != "true"
        or os.getenv("GITHUB_EVENT_NAME") != "push"
        or os.getenv("GITHUB_REF") != "refs/heads/main"
        or os.getenv("GITHUB_REPOSITORY") != REPO
    ):
        report("not_run", "isolated_main_actions_only")
        return 2

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        report("not_run", "missing_openrouter_key")
        return 0

    # This flag affects only this short-lived CI process, never router.service.
    os.environ["ROUTER_OFFLINE_ONLY"] = "0"
    print("LIVE_FREE_ENV=isolated_ci_online_only", flush=True)
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

            # Exactly one real POST through Router; no model fallback or retries.
            result = router.route(RouteRequest(
                task="Reply with exactly " + MARKER,
                required_capabilities=frozenset({"chat"}),
                decompose=False,
                timeout_s=25,
            ))
            if not result.ok or not result.text or result.provider != "openrouter" or result.model != MODEL:
                report("failed", "router_completion_unsuccessful")
                return 2
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
            print("LIVE_FREE_RESPONSE=" + json.dumps(result.text[:160], ensure_ascii=True), flush=True)
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
