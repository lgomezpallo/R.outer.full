"""Read-only inventory classification. Never discovers models or authorizes spending.

Catalog entries may be built-ins, stale, or imported. A reported zero model
price does not prove an account is protected against billable overages.
"""
from __future__ import annotations

from .registry import RegisteredProvider
from .selector import billing_ineligibility_reason

SUPPORTED_PROVIDERS = ("groq", "nvidia", "cloudflare", "openrouter")


def inventory_report(providers: list[RegisteredProvider]) -> dict:
    """Classify each provider+model independently using the production billing gate.

    "discovered" means present in the most recent successfully merged inventory
    snapshot, not a live API check. No network access or inference is performed.
    """
    registered = {item.spec.id: item for item in providers}
    totals = {
        "cataloged": 0,
        "discovered_in_last_snapshot": 0,
        "reported_free_price": 0,
        "reported_paid_price": 0,
        "unknown_price": 0,
        "eligible": 0,
        "blocked": 0,
    }
    rows = []
    for provider_id in SUPPORTED_PROVIDERS:
        provider = registered.get(provider_id)
        counts = dict.fromkeys(totals, 0)
        models = []
        if provider is not None:
            for model in provider.spec.models:
                price = model.billing_status if model.billing_status in {"free", "paid"} else "unknown"
                reason = billing_ineligibility_reason(provider, model)
                eligible = reason is None
                counts["cataloged"] += 1
                counts["discovered_in_last_snapshot"] += int(model.discovered_in_last_snapshot)
                counts[{"free": "reported_free_price", "paid": "reported_paid_price", "unknown": "unknown_price"}[price]] += 1
                counts["eligible" if eligible else "blocked"] += 1
                models.append({
                    "id": model.id,
                    "discovered_in_last_snapshot": model.discovered_in_last_snapshot,
                    "reported_price": price,
                    "eligible_for_inference": eligible,
                    "blocked_reason": reason,
                })
        for key in totals:
            totals[key] += counts[key]
        rows.append({
            "provider": provider_id,
            "configured": provider is not None,
            "counts": counts,
            "models": models,
        })
    return {
        "scope": "configured_providers_and_local_catalog_only",
        "discovery_freshness": "not_verified",
        "live_discovery_performed": False,
        "account_billing_verified": False,
        "reported_free_price_is_not_no_charge_guarantee": True,
        "totals": totals,
        "providers": rows,
    }
