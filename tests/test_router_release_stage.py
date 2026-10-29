"""Release staging safeguards: local snapshots never mutate the dirty checkout."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_stage.py"
spec = importlib.util.spec_from_file_location("router_release_stage", MODULE)
assert spec is not None and spec.loader is not None
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_candidate_sha_must_be_explicit_and_match_candidate_branch(monkeypatch):
    assert stage.RELEASE_BRANCH == "feat/offline-cooldown-restart-consistency-20261028"
    monkeypatch.delenv("ROUTER_RELEASE_SHA", raising=False)
    with pytest.raises(stage.StageBlocked, match="candidate_sha_required"):
        stage.candidate_pin()
    for invalid in ("339898a", "not-a-sha", "A" * 40, "0" * 39):
        monkeypatch.setenv("ROUTER_RELEASE_SHA", invalid)
        with pytest.raises(stage.StageBlocked, match="candidate_sha_required"):
            stage.candidate_pin()
    monkeypatch.setenv("ROUTER_RELEASE_SHA", "a" * 40)
    assert stage.candidate_pin() == "a" * 40


def test_sidecar_forces_offline_and_never_inherits_provider_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "sensitive-test-placeholder")
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "sensitive-test-token")
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "0")
    monkeypatch.setenv("ROUTER_PROVIDER_PRICING_OVERRIDE", "unsafe")
    monkeypatch.setattr(stage, "port_free", lambda port: True)
    monkeypatch.setattr(stage, "health", lambda port: True)
    observed = []

    class Result:
        returncode = 0

    def fake_run(args, **kwargs):
        observed.append(args)
        return Result()

    monkeypatch.setattr(stage.subprocess, "run", fake_run)
    release = tmp_path / "candidate"
    release.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    assert stage.isolated_sidecar(release, tmp_path / "python", snapshot, "candidate")
    start = observed[0]
    assert "systemd-run" in start
    assert "--property=IPAddressDeny=any" in start
    assert "--property=IPAddressAllow=localhost" in start
    assert "--setenv=ROUTER_OFFLINE_ONLY=1" in start
    assert "--setenv=ROUTER_DB_FILE=" + str(snapshot / "sidecar-candidate.db") in start
    assert "--setenv=ROUTER_CATALOG_FILE=" + str(snapshot / "sidecar-candidate-catalog.json") in start
    assert not any("GROQ_API_KEY" in arg or "ROUTER_SERVICE_TOKEN" in arg
                   or "ROUTER_PROVIDER_PRICING_OVERRIDE" in arg for arg in start)
    assert any("systemctl" in args and "stop" in args for args in observed)


def test_private_directory_rejects_symlink(tmp_path: Path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(stage.StageBlocked, match="unsafe_snapshot_directory"):
        stage.private_directory(link)


def test_online_sqlite_backup_is_consistent(tmp_path: Path):
    import sqlite3

    source = tmp_path / "live.db"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('preserved')")
    copy = tmp_path / "backup.db"
    assert stage.sqlite_snapshot(source, copy)
    with sqlite3.connect(copy) as connection:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "preserved"
    assert (copy.stat().st_mode & 0o777) == 0o600


def test_snapshot_preserves_dirty_tracked_and_untracked(tmp_path: Path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    tracked = repo / "router.py"
    tracked.write_text("original\n")
    git(repo, "add", "router.py")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    tracked.write_text("local-change\n")
    secret = repo / "local-private.env"
    secret.write_text("placeholder-not-a-real-key\n")
    snap = tmp_path / "snapshot"
    stage.private_directory(snap)
    monkeypatch.setattr(stage, "DATA", tmp_path / "not-present")
    result = stage.backup_checkout(repo, snap, base)
    assert result["untracked"] == 1
    assert "local-change" in (snap / "uncommitted.patch").read_text()
    with tarfile.open(snap / "untracked.tar") as archive:
        assert archive.getnames() == ["local-private.env"]
        assert archive.extractfile("local-private.env").read() == b"placeholder-not-a-real-key\n"
    assert tracked.read_text() == "local-change\n"
    assert secret.read_text() == "placeholder-not-a-real-key\n"
    assert (snap / "oracle-head.bundle").stat().st_size > 0
    assert all((p.stat().st_mode & 0o777) == 0o600 for p in snap.iterdir() if p.is_file())


def test_no_production_mutation_commands_in_release_script():
    script = MODULE.read_text()
    for forbidden in (
        "git reset --hard",
        "git clean -fd",
        "systemctl restart",
        "systemctl stop",
        "git checkout -f",
    ):
        assert forbidden not in script


def test_effective_data_paths_use_runtime_environment_without_exposing_keys(tmp_path: Path):
    app = tmp_path / "checkout"
    db = tmp_path / "data" / "router.db"
    catalog = tmp_path / "private" / "catalog-live.json"
    raw = bytes([0]).join((
        b"ROUTER_DB_FILE=" + os.fsencode(db),
        b"ROUTER_CATALOG_FILE=" + os.fsencode(catalog),
        b"GROQ_API_KEY=private-test-placeholder",
    ))
    assert stage.effective_service_data_paths(raw, app) == {"db": db, "catalog": catalog}
    with pytest.raises(stage.StageBlocked, match="effective_data_paths_unverified"):
        stage.effective_service_data_paths(b"ROUTER_DB_FILE=router.db", app)


def test_stage_backup_includes_effective_private_catalog_and_database(tmp_path: Path, monkeypatch):
    import sqlite3

    app = tmp_path / "checkout"
    app.mkdir()
    git(app, "init", "-q")
    git(app, "config", "user.name", "Test")
    git(app, "config", "user.email", "test@example.invalid")
    (app / "router.py").write_text("original\n")
    git(app, "add", "router.py")
    git(app, "commit", "-qm", "base")
    base = git(app, "rev-parse", "HEAD")

    data = tmp_path / "data"
    data.mkdir()
    db = data / "router.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserved')")
    private = tmp_path / "private"
    private.mkdir()
    catalog = private / "catalog-live.json"
    catalog.write_text('{"models":[]}')
    snapshot = tmp_path / "snapshot"
    stage.private_directory(snapshot)
    monkeypatch.setattr(stage, "DATA", tmp_path / "not-present")
    stage.backup_checkout(app, snapshot, base, active={"db": db, "catalog": catalog})
    with sqlite3.connect(snapshot / "effective-router.db") as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("preserved",)
    assert (snapshot / "effective-catalog.json").read_text() == '{"models":[]}'
    assert (snapshot / "effective-catalog.json").stat().st_mode & 0o777 == 0o600
    assert catalog.read_text() == '{"models":[]}'

    catalog.unlink()
    with pytest.raises(stage.StageBlocked, match="effective_catalog_missing_or_unsafe"):
        stage.backup_checkout(app, snapshot, base, active={"db": db, "catalog": catalog})
