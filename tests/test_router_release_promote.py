"""Safeguards for reversible Router release promotion."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_promote.py"
spec = importlib.util.spec_from_file_location("router_release_promote", MODULE)
assert spec is not None and spec.loader is not None
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def test_pin_and_override_are_fixed():
    assert promote.PINNED_SHA == "339898a6081061b41ad2557b600e06efbc1f00ef"
    assert promote.OVERRIDE.name == "99-router-frozen-release.conf"


def test_original_argv_accepts_known_uvicorn_service():
    raw = ("{ path=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python ; "
           "argv[]=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python "
           "-m uvicorn router.api:app --host 127.0.0.1 --port 8010 ; "
           "ignore_errors=no ; start_time=[n/a] }")
    args = promote.original_argv(raw)
    assert args[-2:] == ["--port", "8010"]
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("127.0.0.1", "0.0.0.0"))
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("--port 8010", "--port 8080"))
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("router.api:app", "other.app:app"))


def test_environment_file_parser_does_not_read_values():
    assert promote.environment_files("/etc/router-providers.env (ignore_errors=no)") == [
        "/etc/router-providers.env"
    ]
    with pytest.raises(promote.PromoteBlocked):
        promote.environment_files("garbled")
    with pytest.raises(promote.PromoteBlocked):
        promote.environment_files("/etc/../secret (ignore_errors=no)")


def test_private_directory_rejects_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(promote.PromoteBlocked):
        promote.private_dir(alias)


def test_live_sqlite_snapshot_preserves_source(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('unchanged')")
    copy = tmp_path / "snapshot.db"
    promote.online_snapshot(source, copy)
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("unchanged",)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("unchanged",)
    assert copy.stat().st_mode & 0o777 == 0o600


def test_no_destructive_git_or_database_restore():
    source = MODULE.read_text()
    for forbidden in ("git reset --hard", "git clean -fd", "git checkout -f",
                      "DROP TABLE", "rm -rf", "cp -f router.db"):
        assert forbidden not in source
    assert 'new_argv = argv + ["--app-dir", str(RELEASE)]' in source
    assert "automatic_rollback=" in source
