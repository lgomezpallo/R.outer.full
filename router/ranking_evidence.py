"""Evidence-aware ranking tiers; never turn catalog guesses into verification.

This module changes ranking preference only. Billing authorization and
capability eligibility remain independent fail-closed gates.
"""
from __future__ import annotations

from .capability_evidence import _dimension

_ALIASES = {
    "code": ("code", "coding"),
    "coding": ("code", "coding"),
}


def verification_tier(model, required: frozenset[str]) -> int:
    """0=all recorded verified, 1=unverified, 2=inconclusive, 9=unsupported.

    Legacy migrations, model-name guesses and declarations cannot earn a
    verified bonus. Contradictory records count as unsupported for ranking.
    """
    if not required:
        return 1
    statuses = [
        _dimension(model, _ALIASES.get(capability, (capability,)))["status"]
        for capability in required
    ]
    if any(status in {"recorded_unsupported", "conflicting_records"} for status in statuses):
        return 9
    if all(status == "recorded_verified" for status in statuses):
        return 0
    if "recorded_inconclusive" in statuses:
        return 2
    return 1
