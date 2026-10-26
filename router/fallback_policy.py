"""Offline-safe interpretation of HTTP Retry-After for fallback cooldowns.

No sleeps, probes or provider requests are performed here. A very large
numeric delay is treated conservatively as a long cooldown, not ignored.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

MAX_RETRY_AFTER_SECONDS = 2_147_483_647


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse RFC 9110 delta-seconds or HTTP-date; None means unusable header."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        if len(value) > 10:
            return float(MAX_RETRY_AFTER_SECONDS)
        return float(min(int(value), MAX_RETRY_AFTER_SECONDS))
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if target.tzinfo is None:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now_must_be_timezone_aware")
    seconds = (target.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, float(MAX_RETRY_AFTER_SECONDS)))
