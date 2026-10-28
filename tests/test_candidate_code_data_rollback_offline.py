"""Rehearse candidate code + SQLite schema/data rollback using disposable local fixtures.

No Oracle, external inference, provider credentials, billing, network or systemd.
The existing production release scripts remain pinned to the historical SHA.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from router.storage import RouterStore
from scripts.router_release_stage import sqlite_snapshot

pytestmark = pytest.mark.strict_billing


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _verified_restore(snapshot: Path, target: Path, digest: str) -> None:
    """Only replace a stopped synthetic DB after checking backup and restored copy."""
    if snapshot.is_symlink() or target.is_symlink():
        raise ValueError("unsafe_restore_path")
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != digest:
        raise ValueError("backup_checksum_mismatch")
    temporary = target.with_name(target.name + ".restore-pending")
    if temporary.exists():
        raise ValueError("restore_target_exists")
    try:
        with sqlite3.connect(snapshot) as saved, sqlite3.connect(temporary) as restored:
            if saved.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup_integrity_failed")
            saved.backup(restored)
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("restore_integrity_failed")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def test_code_schema_data_and_catalog_rollback_on_disposable_copy(tmp_path: Path) -> None:
    repo = tmp_path / "synthetic-git"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Offline CI")
    _git(repo, "config", "user.email", "offline@example.invalid")
    app = repo / "router-version.txt"
    app.write_text("baseline\n")
    _git(repo, "add", "router-version.txt")
    _git(repo, "commit", "-qm", "synthetic baseline")
    baseline_sha = _git(repo, "rev-parse", "HEAD")
    app.write_text("candidate\n")
    _git(repo, "commit", "-qam", "synthetic candidate")
    assert _git(repo, "rev-parse", "HEAD") != baseline_sha

    db = tmp_path / "router-synthetic.db"
    store = RouterStore(db)
    store.record_metric(
        request_id="before", application_name="go-ute", provider_id=None,
        model_id=None, phase="route", attempt_number=0, latency_ms=0,
        success=False, error_type="no_eligible_provider",
    )
    catalog = tmp_path / "router-catalog.json"
    catalog.write_text(json.dumps({"models": ["synthetic-free-disabled"]}))
    saved_catalog = tmp_path / "backup-catalog.json"
    shutil.copy2(catalog, saved_catalog)

    # Snapshot while WAL connection is open, then simulate a candidate migration.
    writer = sqlite3.connect(db)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE synthetic_records (value TEXT NOT NULL)")
        writer.execute("INSERT INTO synthetic_records VALUES ('baseline')")
        writer.execute("PRAGMA user_version=1")
        writer.commit()
        backup = tmp_path / "backup-router.db"
        assert sqlite_snapshot(db, backup) is True
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()

        writer.execute("ALTER TABLE synthetic_records ADD COLUMN candidate_only INTEGER")
        writer.execute("INSERT INTO synthetic_records VALUES ('candidate', 1)")
        writer.execute("PRAGMA user_version=2")
        writer.commit()
    finally:
        writer.close()
    catalog.write_text(json.dumps({"models": ["synthetic-changed"]}))
    store.record_metric(
        request_id="candidate", application_name="go-ute", provider_id=None,
        model_id=None, phase="route", attempt_number=0, latency_ms=0,
        success=False, error_type="candidate",
    )

    # Simulate stopping the service and reverting the code before restoring data.
    _git(repo, "checkout", "--detach", "-q", baseline_sha)
    assert app.read_text() == "baseline\n"
    assert _git(repo, "rev-parse", "HEAD") == baseline_sha
    assert _git(repo, "status", "--porcelain") == ""

    _verified_restore(backup, db, backup_hash)
    pending_catalog = tmp_path / "catalog.restore-pending"
    shutil.copy2(saved_catalog, pending_catalog)
    os.replace(pending_catalog, catalog)
    with sqlite3.connect(db) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [row[1] for row in restored.execute(
            "PRAGMA table_info(synthetic_records)"
        )] == ["value"]
        assert restored.execute("SELECT value FROM synthetic_records").fetchall() == [
            ("baseline",)
        ]
        assert restored.execute("SELECT request_id FROM request_metrics").fetchall() == [
            ("before",)
        ]
        assert restored.execute("SELECT name FROM sqlite_master WHERE name='provider_runtime'").fetchone()
    assert catalog.read_bytes() == saved_catalog.read_bytes()
    assert backup.exists()
    assert (db.stat().st_mode & 0o777) == 0o600


def test_corrupt_backup_refuses_restore_without_changing_active_copy(tmp_path: Path) -> None:
    db = tmp_path / "synthetic.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.execute("INSERT INTO records VALUES ('must-survive')")
    backup = tmp_path / "backup.db"
    assert sqlite_snapshot(db, backup)
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    backup.write_bytes(b"corrupted synthetic backup")
    with pytest.raises(ValueError, match="backup_checksum_mismatch"):
        _verified_restore(backup, db, digest)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM records").fetchall() == [
            ("must-survive",)
        ]
    assert not (tmp_path / "synthetic.db.restore-pending").exists()
