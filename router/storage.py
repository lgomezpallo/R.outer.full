from __future__ import annotations
from dataclasses import dataclass
import base64
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Iterable

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - dependency is installed in production
    AESGCM = None

@dataclass(frozen=True)
class PersistedCredential:
    provider_id: str
    api_key: str

class RouterStore:
    """Zero-cost local persistence. Secrets are encrypted; tokens are hashed."""

    def __init__(self, path: str | Path = "router.db", master_key: str | None = None) -> None:
        self.path = str(path)
        self.master_key = master_key or os.getenv("ROUTER_MASTER_KEY", "")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_credentials (
                    provider_id TEXT PRIMARY KEY,
                    ciphertext TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS request_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    application_name TEXT NOT NULL,
                    provider_id TEXT,
                    model_id TEXT,
                    phase TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    error_type TEXT,
                    error_code INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS request_metrics_provider_idx
                    ON request_metrics(provider_id, model_id, created_at);
                CREATE INDEX IF NOT EXISTS request_metrics_request_idx
                    ON request_metrics(request_id);
                CREATE TABLE IF NOT EXISTS model_runtime (
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    cooldown_strikes INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    PRIMARY KEY(provider_id, model_id)
                );
                CREATE TABLE IF NOT EXISTS provider_runtime (
                    provider_id TEXT PRIMARY KEY,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    ewma_latency_ms REAL,
                    cooldown_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    health_ok INTEGER,
                    cooldown_strikes INTEGER NOT NULL DEFAULT 0,
                    last_failure_kind TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_budget (\n                    provider_id TEXT NOT NULL, day_utc TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY (provider_id,day_utc)\n                );\n                CREATE TABLE IF NOT EXISTS audit_model_claims (
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (provider_id, model_id, capability)
                );
                CREATE TABLE IF NOT EXISTS app_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    preview TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(provider_runtime)").fetchall()}
            if "cooldown_strikes" not in columns:
                conn.execute("ALTER TABLE provider_runtime ADD COLUMN cooldown_strikes INTEGER NOT NULL DEFAULT 0")
            if "last_failure_kind" not in columns:
                conn.execute("ALTER TABLE provider_runtime ADD COLUMN last_failure_kind TEXT")

    def _key_bytes(self) -> bytes:
        if not self.master_key:
            raise RuntimeError("router_master_key_required")
        raw = self.master_key.strip()
        try:
            if len(raw) == 64:
                key = bytes.fromhex(raw)
            else:
                key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except Exception as exc:
            raise RuntimeError("invalid_router_master_key") from exc
        if len(key) != 32:
            raise RuntimeError("invalid_router_master_key")
        return key

    @property
    def can_persist_secrets(self) -> bool:
        try:
            self._key_bytes()
            return AESGCM is not None
        except RuntimeError:
            return False

    def save_credential(self, provider_id: str, api_key: str) -> None:
        if AESGCM is None:
            raise RuntimeError("cryptography_dependency_missing")
        key = self._key_bytes()
        nonce = secrets.token_bytes(12)
        aad = provider_id.encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, api_key.encode("utf-8"), aad)
        preview = f"{api_key[:3]}...{api_key[-3:]}" if len(api_key) >= 8 else "***"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_credentials(provider_id,ciphertext,nonce,preview)
                VALUES(?,?,?,?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    ciphertext=excluded.ciphertext,
                    nonce=excluded.nonce,
                    preview=excluded.preview,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    provider_id,
                    base64.b64encode(ciphertext).decode("ascii"),
                    base64.b64encode(nonce).decode("ascii"),
                    preview,
                ),
            )

    def load_credentials(self) -> list[PersistedCredential]:
        if not self.can_persist_secrets:
            return []
        key = self._key_bytes()
        credentials: list[PersistedCredential] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider_id,ciphertext,nonce FROM provider_credentials"
            ).fetchall()
        for row in rows:
            try:
                plaintext = AESGCM(key).decrypt(
                    base64.b64decode(row["nonce"]),
                    base64.b64decode(row["ciphertext"]),
                    row["provider_id"].encode("utf-8"),
                )
            except Exception:
                continue
            credentials.append(PersistedCredential(row["provider_id"], plaintext.decode("utf-8")))
        return credentials

    def record_metric(
        self,
        *,
        request_id: str,
        application_name: str,
        provider_id: str | None,
        model_id: str | None,
        phase: str,
        attempt_number: int,
        latency_ms: int,
        success: bool,
        error_type: str | None = None,
        error_code: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO request_metrics(
                    request_id,application_name,provider_id,model_id,phase,
                    attempt_number,latency_ms,success,error_type,error_code
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id, application_name, provider_id, model_id, phase,
                    attempt_number, latency_ms, 1 if success else 0, error_type, error_code,
                ),
            )

    def recent_request_traces(self, limit: int = 30, application_name: str | None = None) -> list[dict]:
        """Recent per-request attempt traces without prompts, responses or credentials."""
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            if application_name:
                ids = conn.execute(
                    """SELECT request_id, MAX(id) AS last_id FROM request_metrics
                       WHERE application_name = ?
                       GROUP BY request_id ORDER BY last_id DESC LIMIT ?""",
                    (application_name, limit),
                ).fetchall()
            else:
                ids = conn.execute(
                    """SELECT request_id, MAX(id) AS last_id FROM request_metrics
                       GROUP BY request_id ORDER BY last_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"""SELECT request_id, application_name, provider_id, model_id, phase,
                           attempt_number, latency_ms, success, error_type, error_code, created_at
                    FROM request_metrics WHERE request_id IN ({placeholders})
                    ORDER BY id ASC""",
                tuple(row["request_id"] for row in ids),
            ).fetchall()
        traces = {}
        for row in rows:
            rid = row["request_id"]
            trace = traces.setdefault(rid, {
                "request_id": rid, "application_name": row["application_name"],
                "created_at": row["created_at"], "attempts": [],
                "total_provider_latency_ms": 0, "fallback": False,
            })
            trace["attempts"].append({
                "provider": row["provider_id"], "model": row["model_id"],
                "phase": row["phase"], "attempt_number": row["attempt_number"],
                "latency_ms": row["latency_ms"], "success": bool(row["success"]),
                "error_type": row["error_type"], "error_code": row["error_code"],
            })
            trace["total_provider_latency_ms"] += int(row["latency_ms"])
        for trace in traces.values():
            trace["fallback"] = len({a["provider"] for a in trace["attempts"]}) > 1
        return [traces[row["request_id"]] for row in ids]

    def recent_model_stats(self, limit: int = 2000) -> dict[tuple[str, str], dict]:
        """Rebuild per-model routing observations without treating probes as answers.

        Keep the most recent N *routing attempts*, not N arbitrary diagnostics.
        Latency is the chronological success-only EWMA used by RuntimeState.
        A failed request has no successful completion latency.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provider_id, model_id, success, latency_ms
                FROM request_metrics
                WHERE provider_id IS NOT NULL AND model_id IS NOT NULL
                  AND (phase IN ('direct', 'execute', 'plan', 'verify', 'compose')
                       OR phase LIKE 'subtask:%')
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 10000)),),
            ).fetchall()
        buckets: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            buckets.setdefault((row["provider_id"], row["model_id"]), []).append(row)
        result: dict[tuple[str, str], dict] = {}
        for key, items in buckets.items():
            successes = sum(int(item["success"]) for item in items)
            latency = None
            # Rows arrive newest first; EWMA must replay oldest to newest.
            for item in reversed(items):
                if int(item["success"]):
                    measured = float(item["latency_ms"])
                    latency = measured if latency is None else latency * 0.7 + measured * 0.3
            result[key] = {
                "success_count": successes,
                "failure_count": len(items) - successes,
                "success_rate": successes / len(items),
                "latency_ms": latency,
                "samples": float(len(items)),
            }
        return result

    def reserve_audit_budget(self, provider_id: str, daily_limit: int) -> tuple[bool, int]:
        """Atomically reserve one external probe for the current UTC day."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO audit_budget(provider_id, day_utc, used) VALUES (?, ?, 0)",
                (provider_id, today),
            )
            conn.execute(
                "UPDATE audit_budget SET used=used+1 WHERE provider_id=? AND day_utc=? AND used < ?",
                (provider_id, today, daily_limit),
            )
            updated = conn.execute("SELECT changes()").fetchone()[0] == 1
            used = conn.execute(
                "SELECT used FROM audit_budget WHERE provider_id=? AND day_utc=?",
                (provider_id, today),
            ).fetchone()[0]
        return updated, used

    def reserve_audit_model(
        self, provider_id: str, model_id: str, capability: str, daily_limit: int
    ) -> tuple[str, int]:
        """Atomically claim one model/capability and one internal daily slot.

        A claim is durable across days and processes: after a crash or an
        ambiguous probe result, retrying automatically could spend again.
        This method grants no external inference or billing authorization.
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO audit_budget(provider_id, day_utc, used) VALUES (?, ?, 0)",
                (provider_id, today),
            )
            used = int(conn.execute(
                "SELECT used FROM audit_budget WHERE provider_id=? AND day_utc=?",
                (provider_id, today),
            ).fetchone()[0])
            if conn.execute(
                "SELECT 1 FROM audit_model_claims WHERE provider_id=? AND model_id=? AND capability=?",
                (provider_id, model_id, capability),
            ).fetchone() is not None:
                return "already_claimed", used
            if used >= max(0, int(daily_limit)):
                return "budget_exhausted", used
            conn.execute(
                "INSERT INTO audit_model_claims(provider_id, model_id, capability) VALUES (?, ?, ?)",
                (provider_id, model_id, capability),
            )
            conn.execute(
                "UPDATE audit_budget SET used=used+1 WHERE provider_id=? AND day_utc=?",
                (provider_id, today),
            )
            return "reserved", used + 1

    def audit_claimed_models(self, provider_id: str, capability: str) -> set[str]:
        """Read durable audit claims without reserving slots or calling providers.

        A claimed model remains unavailable for automatic re-audit even when
        evidence is missing after a failed or interrupted attempt.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT model_id FROM audit_model_claims WHERE provider_id=? AND capability=?",
                (provider_id, capability),
            ).fetchall()
        return {row["model_id"] for row in rows}

    def audit_budget_used(self) -> dict[str, int]:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            rows = conn.execute("SELECT provider_id,used FROM audit_budget WHERE day_utc=?", (today,)).fetchall()
        return {row["provider_id"]: row["used"] for row in rows}

    def model_performance_report(self, limit: int = 2000) -> list[dict]:
        """Routing-attempt reliability and successful-completion latency.

        Failed attempt durations are useful for troubleshooting, but must not
        inflate the latency reported for successful model completions.
        Probe and audit phases are excluded, just as in recent_model_stats.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT provider_id, model_id, success, latency_ms, error_type
                   FROM request_metrics
                   WHERE provider_id IS NOT NULL AND model_id IS NOT NULL
                     AND (phase IN ('direct', 'execute', 'plan', 'verify', 'compose')
                          OR phase LIKE 'subtask:%')
                   ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit), 10000)),),
            ).fetchall()
        groups: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["provider_id"], row["model_id"])
            entry = groups.setdefault(key, {
                "provider": key[0], "model": key[1], "attempts": 0,
                "successful_requests": 0, "total_latency_ms": 0,
                "successful_latency_ms": 0, "errors": {},
            })
            entry["attempts"] += 1
            entry["total_latency_ms"] += int(row["latency_ms"])
            if row["success"]:
                entry["successful_requests"] += 1
                entry["successful_latency_ms"] += int(row["latency_ms"])
            else:
                kind = row["error_type"] or "unknown"
                entry["errors"][kind] = entry["errors"].get(kind, 0) + 1
        output = []
        for entry in groups.values():
            n = entry["attempts"]
            output.append({
                "provider": entry["provider"], "model": entry["model"],
                "attempts": n, "successful_requests": entry["successful_requests"],
                "success_rate": round(entry["successful_requests"] / n, 4),
                # None means no successful completion was observed.
                "avg_latency_ms": (
                    round(entry["successful_latency_ms"] / entry["successful_requests"])
                    if entry["successful_requests"] else None
                ),
                "latency_samples": entry["successful_requests"],
                "latency_scope": "successful_completions_only",
                "avg_attempt_duration_ms": round(entry["total_latency_ms"] / n),
                "errors": entry["errors"],
            })
        return sorted(output, key=lambda item: (-item["attempts"], item["provider"], item["model"]))


    def routing_fallback_report(self, limit: int = 200) -> dict:
        """Read-only request-level retry/fallback outcomes from local SQLite.

        Select recent *request IDs*, then all their stored routing attempts,
        so a multi-attempt request is not truncated at the sampling boundary.
        Probe/audit phases are excluded. In-flight requests may be incomplete
        at snapshot time; these are observations, not quality or billing proof.
        """
        bounded = max(1, min(int(limit), 500))
        routing_phase = """(phase IN ('direct', 'execute', 'plan', 'verify', 'compose')
                            OR phase LIKE 'subtask:%')"""
        with self._connect() as conn:
            rows = conn.execute(
                f"""WITH recent AS (
                        SELECT request_id FROM request_metrics
                        WHERE {routing_phase}
                        GROUP BY request_id ORDER BY MAX(id) DESC LIMIT ?
                    )
                    SELECT m.request_id, m.provider_id, m.model_id, m.success
                    FROM request_metrics AS m
                    JOIN recent AS r ON r.request_id = m.request_id
                    WHERE (m.phase IN ('direct', 'execute', 'plan', 'verify', 'compose')
                           OR m.phase LIKE 'subtask:%')
                    ORDER BY m.id ASC""",
                (bounded,),
            ).fetchall()
        requests: dict[str, list[tuple[str | None, str | None, bool]]] = {}
        for row in rows:
            requests.setdefault(row["request_id"], []).append(
                (row["provider_id"], row["model_id"], bool(row["success"]))
            )
        attempts = sum(len(items) for items in requests.values())
        successes = sum(sum(1 for item in items if item[2]) for items in requests.values())
        request_successes = sum(1 for items in requests.values() if items[-1][2])
        retry_requests = sum(
            1 for items in requests.values()
            if len({(provider, model) for provider, model, _ in items}) < len(items)
        )
        model_fallbacks = sum(
            1 for items in requests.values()
            if len({(provider, model) for provider, model, _ in items}) > 1
        )
        provider_fallbacks = sum(
            1 for items in requests.values()
            if len({provider for provider, _, _ in items}) > 1
        )
        recovered = sum(
            1 for items in requests.values()
            if items[-1][2] and any(not item[2] for item in items[:-1])
        )
        count = len(requests)
        return {
            "sampled_requests": count,
            "attempts": attempts,
            "successful_attempts": successes,
            "successful_requests": request_successes,
            "failed_requests": count - request_successes,
            "requests_with_same_model_retry": retry_requests,
            "requests_with_model_fallback": model_fallbacks,
            "requests_with_provider_fallback": provider_fallbacks,
            "requests_recovered_after_failure": recovered,
            "attempt_success_rate": round(successes / attempts, 4) if attempts else None,
            "request_success_rate": round(request_successes / count, 4) if count else None,
            "observation_scope": "recent_routing_request_ids_only",
            "snapshot_caveat": "in_flight_requests_may_be_incomplete",
            "quality_measured": False,
            "billing_verified": False,
        }

    def provider_routing_report(self, limit: int = 2000) -> list[dict]:
        """Provider totals over exactly the same routing attempts as model reports.

        Do not use provider_runtime counters for these totals: those counters
        also reflect active health checks and are not a routing-only sample.
        This report never changes cooldowns, health, or persisted state.
        """
        groups: dict[str, dict] = {}
        for model in self.model_performance_report(limit=limit):
            provider_id = model["provider"]
            row = groups.setdefault(provider_id, {
                "provider": provider_id,
                "attempts": 0,
                "successful_requests": 0,
                "failed_attempts": 0,
                "models_observed": 0,
                "errors": {},
            })
            row["attempts"] += model["attempts"]
            row["successful_requests"] += model["successful_requests"]
            row["failed_attempts"] += model["attempts"] - model["successful_requests"]
            row["models_observed"] += 1
            for error_type, count in model["errors"].items():
                row["errors"][error_type] = row["errors"].get(error_type, 0) + count

        for row in groups.values():
            row["success_rate"] = round(
                row["successful_requests"] / row["attempts"], 4
            )
            row["observation_scope"] = "routing_attempts_only"
        return sorted(groups.values(), key=lambda row: row["provider"])

    def create_app_token(self, name: str) -> str:
        token = "rtr_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        preview = token[:8] + "..." + token[-4:]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_tokens(name,token_hash,preview) VALUES(?,?,?)",
                (name, digest, preview),
            )
        return token

    def verify_app_token(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM app_tokens WHERE token_hash=? AND active=1",
                (digest,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE app_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE token_hash=?",
                    (digest,),
                )
                return str(row["name"])
        return None

    def list_app_tokens(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name,preview,active,last_used_at,created_at FROM app_tokens ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]


    def save_model_runtime(self, provider_id: str, model_id: str, state) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO model_runtime(provider_id,model_id,cooldown_until,cooldown_strikes,last_error)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(provider_id,model_id) DO UPDATE SET
                   cooldown_until=excluded.cooldown_until,
                   cooldown_strikes=excluded.cooldown_strikes,
                   last_error=excluded.last_error""",
                (provider_id, model_id, state.cooldown_until, state.cooldown_strikes, state.last_error),
            )

    def load_model_runtime(self, provider_id: str) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT model_id,cooldown_until,cooldown_strikes,last_error FROM model_runtime WHERE provider_id=?",
                (provider_id,),
            ).fetchall()
        return {row["model_id"]: dict(row) for row in rows}

    def save_provider_runtime(self, provider_id: str, state) -> None:
        health_value = None if state.health_ok is None else (1 if state.health_ok else 0)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_runtime(
                    provider_id,success_count,failure_count,ewma_latency_ms,
                    cooldown_until,last_error,health_ok,cooldown_strikes,last_failure_kind
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    ewma_latency_ms=excluded.ewma_latency_ms,
                    cooldown_until=excluded.cooldown_until,
                    last_error=excluded.last_error,
                    health_ok=excluded.health_ok,
                    cooldown_strikes=excluded.cooldown_strikes,
                    last_failure_kind=excluded.last_failure_kind,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    provider_id,
                    state.success_count,
                    state.failure_count,
                    state.ewma_latency_ms,
                    state.cooldown_until,
                    state.last_error,
                    health_value,
                    state.cooldown_strikes,
                    state.last_failure_kind,
                ),
            )

    def load_provider_runtime(self, provider_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT success_count,failure_count,ewma_latency_ms,cooldown_until,last_error,health_ok,cooldown_strikes,last_failure_kind
                FROM provider_runtime WHERE provider_id=?
                """,
                (provider_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["health_ok"] is not None:
            result["health_ok"] = bool(result["health_ok"])
        return result
