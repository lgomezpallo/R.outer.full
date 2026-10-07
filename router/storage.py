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

    def recent_model_stats(self, limit: int = 2000) -> dict[tuple[str,str], dict[str,float]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT provider_id,model_id,success,latency_ms
                FROM request_metrics
                WHERE provider_id IS NOT NULL AND model_id IS NOT NULL
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        buckets: dict[tuple[str,str], list[sqlite3.Row]] = {}
        for row in rows:
            buckets.setdefault((row["provider_id"], row["model_id"]), []).append(row)
        result: dict[tuple[str,str], dict[str,float]] = {}
        for key, items in buckets.items():
            success_rate = sum(int(x["success"]) for x in items) / len(items)
            latency = sum(int(x["latency_ms"]) for x in items) / len(items)
            result[key] = {"success_rate": success_rate, "latency_ms": latency, "samples": float(len(items))}
        return result

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
