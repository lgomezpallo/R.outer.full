from datetime import datetime, timezone
import pytest
from router.temporal import temporal_context, with_temporal_context


def test_buenos_aires_clock_converts_utc():
    instant = datetime(2026, 10, 10, 5, 15, tzinfo=timezone.utc)
    context = temporal_context(now=instant)
    assert "Fecha local: 2026-10-10" in context
    assert "Hora local: 02:15:00" in context
    assert "America/Argentina/Buenos_Aires" in context


def test_local_date_follows_timezone_not_utc_date():
    instant = datetime(2026, 10, 11, 1, 30, tzinfo=timezone.utc)
    assert "Fecha local: 2026-10-10" in temporal_context(now=instant)


def test_preserves_conversation_context():
    instant = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
    result = with_temporal_context("user: River", now=instant)
    assert result.endswith("user: River")
    assert result.startswith("[CONTEXTO TEMPORAL DEL SISTEMA]")


def test_naive_clock_rejected():
    with pytest.raises(ValueError, match="clock_must_be_timezone_aware"):
        temporal_context(now=datetime(2026, 10, 10))
