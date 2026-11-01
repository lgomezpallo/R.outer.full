"""Release-gate rehearsals against the actual G.O. HTTP shape and a mocked service rollback.

All service commands are intercepted. No Oracle, provider API, network, billing,
systemd, or real credentials are touched. These are offline simulations, NOT
proof that a production deployment or database rollback is safe.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from router import api
from router.client import ProviderClient
from router.router import Router
from router.storage import RouterStore
MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_promote.py"
spec = importlib.util.spec_from_file_location("router_release_promote_release_gate", MODULE)
assert spec is not None and spec.loader is not None
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


pytestmark = pytest.mark.strict_billing


def test_actual_go_ute_probe_contract_offline(monkeypatch, tmp_path):
    """G.O. probe_contract() expects anonymous 401 and authorized invalid-body 422."""
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "synthetic-go-token")
    monkeypatch.delenv("ROUTER_IACHAT_TOKEN", raising=False)
    monkeypatch.setattr(api, "router", Router(client=ProviderClient()))
    monkeypatch.setattr(api, "store", RouterStore(tmp_path / "go-probe.db"))
    client = TestClient(api.app)

    anonymous = client.post("/route", json={})
    authorized = client.post(
        "/route", json={}, headers={"Authorization": "Bearer synthetic-go-token"}
    )
    assert (anonymous.status_code, authorized.status_code) == (401, 422)


def test_actual_go_ute_answer_payload_offline(monkeypatch, tmp_path):
    """G.O. sends application_name=go-ute, conversation and required chat."""
    monkeypatch.setenv("ROUTER_OFFLINE_ONLY", "1")
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "synthetic-go-token")
    monkeypatch.delenv("ROUTER_IACHAT_TOKEN", raising=False)
    monkeypatch.setattr(api, "router", Router(client=ProviderClient()))
    monkeypatch.setattr(api, "store", RouterStore(tmp_path / "go-route.db"))
    client = TestClient(api.app)

    response = client.post(
        "/route",
        headers={"Authorization": "Bearer synthetic-go-token"},
        json={
            "task": "Estado de obra 1190",
            "conversation": [{"role": "user", "content": "Hola"}],
            "context": "Sos G.O., asistente de gestion de obras UTE.",
            "required_capabilities": ["chat"],
            "application_name": "go-ute",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["error"] == "no_eligible_provider"
    assert result["text"] is None
    assert result["attempts"] == []


def test_mocked_failed_promotion_rolls_back_service_override(monkeypatch, tmp_path, capsys):
    """Inject post-switch health failure; assert previous service is restored.

    Does not execute the real, pinned production promotion script or restore a
    production database. The synthetic data file must remain untouched.
    """
    app = tmp_path / "app"
    app.mkdir()
    backups = tmp_path / "private-backups"
    override = tmp_path / "systemd" / "zzzz-router-frozen-release.conf"
    baseline_data = tmp_path / "baseline.db"
    baseline_data.write_bytes(b"synthetic-database-marker")
    before = baseline_data.read_bytes()
    state = {"running": "baseline", "backup": False, "verified": False}
    commands = []

    monkeypatch.setattr(promote, "PINNED_SHA", "a" * 40)
    monkeypatch.setattr(promote, "APP", app)
    monkeypatch.setattr(promote, "RELEASE", tmp_path / "candidate")
    monkeypatch.setattr(promote, "BACKUPS", backups)
    monkeypatch.setattr(promote, "OVERRIDE", override)
    monkeypatch.setattr(promote.getpass, "getuser", lambda: "operator")

    def fake_command(*args):
        if args[:2] == ("git", "-C"):
            return promote.PINNED_SHA
        assert args[:3] == ("systemctl", "show", "router.service"), args
        assert args[3] == "-p" and args[-1] == "--value"
        property_name = args[4]
        values = {
            "WorkingDirectory": str(app),
            "User": "operator",
            "EnvironmentFiles": "/tmp/synthetic-router.env (ignore_errors=no)",
            "Environment": "",
            "DropInPaths": "/etc/systemd/system/router.service.d/99-existing-custom.conf",
            "ExecStart": (
                "{ path=" + str(app / ".venv/bin/python") + " ; argv[]="
                + str(app / ".venv/bin/python")
                + " -m uvicorn router.api:app --host 127.0.0.1 --port 8010 ; "
                "ignore_errors=no ; start_time=[n/a] }"
            ),
        }
        return values[property_name]

    def fake_subprocess_run(args, **kwargs):
        if args[:3] == ["sudo", "-n", "test"]:
            if args[3] == "!" and args[4] in ("-e", "-L"):
                return SimpleNamespace(returncode=0 if args[5] != "/" else 1)
            if args[3] in ("-e", "-L"):
                return SimpleNamespace(returncode=0 if args[4] == "/" else 1)
        if args == ["systemctl", "cat", "router.service"]:
            kwargs["stdout"].write(b"[Service]\nExecStart=baseline\n")
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"unexpected subprocess: {args!r}")

    def fake_private_unit(script, work, user, envfiles, mode):
        assert mode == "backup", "post-switch real inference verification must not run"
        # Model the real backup contract: both SQLite and catalog must exist
        # before the candidate is allowed to start on isolated data paths.
        with sqlite3.connect(work / "predeploy-router.db") as db:
            db.execute("CREATE TABLE baseline (value TEXT)")
            db.execute("INSERT INTO baseline VALUES ('preserved')")
        (work / "predeploy-catalog.json").write_text('{"models":[]}')
        state["backup"] = True

    def fake_run(*args):
        commands.append(args)
        assert args[:2] == ("sudo", "-n")
        if args[2:4] == ("mkdir", "-p"):
            Path(args[4]).mkdir(parents=True, exist_ok=True)
        elif args[2:5] == ("install", "-m", "0644"):
            shutil.copy2(args[5], args[6])
        elif args[2] == "ln":
            # Atomic no-clobber installation: existing paths must never be replaced.
            assert args[3] == "-T", "drop-in link must not follow directory symlinks"
            os.link(args[4], args[5])
        elif args[2:4] == ("rm", "-f"):
            Path(args[4]).unlink(missing_ok=True)
        elif args[2:4] == ("systemctl", "daemon-reload"):
            pass
        elif args[2:4] == ("systemctl", "restart"):
            state["running"] = "candidate" if override.exists() else "baseline"
        else:
            raise AssertionError(f"unexpected service command: {args!r}")

    monkeypatch.setattr(promote, "command", fake_command)
    monkeypatch.setattr(promote.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(promote, "private_unit", fake_private_unit)
    monkeypatch.setattr(promote, "run", fake_run)
    monkeypatch.setattr(promote, "wait_health", lambda: state["running"] == "baseline")
    monkeypatch.setattr(
        promote, "pinned_process",
        lambda: pytest.fail("candidate process must not be accepted after failed health"),
    )

    with pytest.raises(promote.PromoteBlocked, match="postdeploy_health_or_pin_failed"):
        promote.promote(tmp_path / "not-executed.py")

    assert state["backup"] is True
    assert state["running"] == "baseline"
    assert not override.exists()
    assert baseline_data.read_bytes() == before
    assert "automatic_rollback=passed" in capsys.readouterr().out
    assert any(cmd[2:4] == ("systemctl", "restart") for cmd in commands)


def test_mocked_rollback_restart_failure_does_not_report_success(monkeypatch, tmp_path):
    monkeypatch.setattr(promote, "OVERRIDE", tmp_path / "synthetic.conf")
    calls = []

    def failing_run(*args):
        calls.append(args)
        if args[2:4] == ("systemctl", "restart"):
            raise OSError("simulated restart failure")

    monkeypatch.setattr(promote, "run", failing_run)
    monkeypatch.setattr(
        promote, "wait_health",
        lambda: pytest.fail("failed restart cannot be declared healthy"),
    )
    assert promote.rollback() is False
    assert any(cmd[2:4] == ("systemctl", "restart") for cmd in calls)
