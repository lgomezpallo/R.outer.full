from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Argentina/Buenos_Aires"


def temporal_context(*, now: datetime | None = None, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    """Generate authoritative query-time context without asking a model for the date."""
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("clock_must_be_timezone_aware")
    local = clock.astimezone(ZoneInfo(timezone_name))
    return (
        "[CONTEXTO TEMPORAL DEL SISTEMA]\n"
        f"Fecha local: {local.date().isoformat()}\n"
        f"Hora local: {local.strftime('%H:%M:%S')}\n"
        f"Zona horaria: {timezone_name}\n"
        "Interpretá hoy, mañana, este fin de semana y próximo con referencia a esta fecha. "
        "Para eventos y datos cambiantes, no uses la fecha como prueba de actualidad: "
        "verificá con una fuente externa antes de afirmar resultados. "
        "Si no hay herramienta para verificar, indicá que no está verificado y no inventes.\n"
        "[FIN CONTEXTO TEMPORAL]"
    )


def with_temporal_context(context: str, *, now: datetime | None = None) -> str:
    return temporal_context(now=now) + "\n" + context
