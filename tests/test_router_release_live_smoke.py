"""Guards for the frozen-release live-provider smoke (no Oracle access in CI)."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_live_smoke.py"
spec = importlib.util.spec_from_file_location("router_release_live_smoke", MODULE)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_pin_and_private_output_allowlist(monkeypatch):
    monkeypatch.setattr(smoke, "PINNED_SHA", "")
    with pytest.raises(smoke.SmokeBlocked, match="candidate_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "PINNED_SHA", "339898a")
    with pytest.raises(smoke.SmokeBlocked, match="candidate_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "PINNED_SHA", "a" * 40)
    monkeypatch.setattr(smoke, "SCRIPT_SHA", "")
    with pytest.raises(smoke.SmokeBlocked, match="script_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "SCRIPT_SHA", "b" * 40)
    assert smoke.candidate_pin() == "a" * 40
    assert smoke.VALID_STATUSES == ("live_health=passed", "live_chat=passed", "live_fallback=passed")
    assert smoke.SAFE_FALLBACK_DIAGNOSTICS == frozenset({
        "single_provider", "no_attempts", "first_provider_not_attempted",
        "no_second_attempt", "second_provider_failed", "unexpected_result",
    })


def test_environment_file_parser():
    assert smoke.environment_files(
        "/etc/router-providers.env (ignore_errors=no) /etc/iachat-router.env (ignore_errors=yes)"
    ) == ["/etc/router-providers.env", "/etc/iachat-router.env"]
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("")
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("/etc/../secrets (ignore_errors=no)")
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("garbled data")


def test_private_dir_rejects_symlink(tmp_path):
    actual = tmp_path / "real"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(smoke.SmokeBlocked):
        smoke.private_dir(alias)


def test_sqlite_online_snapshot_does_not_change_source(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE items (name TEXT)")
        db.execute("INSERT INTO items VALUES ('kept')")
    target = tmp_path / "snapshot.db"
    smoke.backup_sqlite(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT name FROM items").fetchone() == ("kept",)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone() == (1,)
    assert target.stat().st_mode & 0o777 == 0o600


def test_live_smoke_cannot_deploy_or_mutate_checkout():
    source = MODULE.read_text()
    assert 'SCRIPT_SHA = os.environ.get("ROUTER_SCRIPT_SHA", "")' in source
    assert 'PINNED_SHA = os.environ.get("ROUTER_RELEASE_SHA", "")' in source
    assert '"--property=Environment=ROUTER_SCRIPT_SHA=" + SCRIPT_SHA' in source
    assert 'os.environ["ROUTER_OFFLINE_ONLY"] = "0"' in source
    assert 'models[0].billing_status != "free"' in source
    assert 'billing_ineligibility_reason(provider, models[0])' in source
    assert 'promote_module.effective_live_data_paths()' in source
    assert "candidate_pin()" in source
    assert "fallback_decisions = rank(api.router.registry.all(), request)" in source
    assert "live_fallback_diagnostic=" in source
    assert "for reason in sorted(SAFE_FALLBACK_DIAGNOSTICS):" in source
    for forbidden in (
        "git reset --hard", "git clean -fd", "systemctl restart router.service",
        "systemctl stop router.service", "git checkout -f",
    ):
        assert forbidden not in source
    assert 'os.environ["ROUTER_DB_FILE"] = str(isolated_db)' in source
    assert 'os.environ["ROUTER_CATALOG_FILE"] = str(work / "smoke-catalog.json")' in source
