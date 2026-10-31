"""Offline regression for the read-only Oracle release preflight.

All external commands are faked; this test never contacts Oracle or providers.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oracle_preflight.sh"


def _fake_command(folder: Path, name: str, script: str) -> None:
    target = folder / name
    target.write_text("#!/bin/sh\n" + script)
    target.chmod(0o700)


def test_oracle_preflight_redacts_inline_execstart_secrets(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    app = tmp_path / "router"
    (app / ".git").mkdir(parents=True)
    (app / ".venv" / "bin").mkdir(parents=True)
    python = app / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)

    _fake_command(fake_bin, "systemctl", r'''
case "$*" in
  "is-active router") echo active ;;
  "show router -p FragmentPath --value") echo /etc/systemd/system/router.service ;;
  "show router -p WorkingDirectory --value") echo /srv/router ;;
  "show router -p User --value") echo ubuntu ;;
  "show router -p ExecStart --value")
    echo 'argv[]=/srv/router/.venv/bin/python -m uvicorn router.api:app --host 127.0.0.1 --port 8010 --api-key INLINE_SECRET_DO_NOT_LOG' ;;
  *) exit 2 ;;
esac
''')
    _fake_command(fake_bin, "curl", "echo '{\"status\":\"ok\"}'\n")
    _fake_command(fake_bin, "git", r'''
case "$*" in
  *"rev-parse HEAD") echo 339898a6081061b41ad2557b600e06efbc1f00ef ;;
  *"status --porcelain") : ;;
  *) exit 2 ;;
esac
''')

    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["ROUTER_APP_DIR"] = str(app)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert "service_exec_layout: router_api" in result.stdout
    assert "service_loopback_bind_hint: yes" in result.stdout
    assert "preflight_read_only_complete" in result.stdout
    assert "INLINE_SECRET_DO_NOT_LOG" not in result.stdout + result.stderr
    assert "--api-key" not in result.stdout + result.stderr
