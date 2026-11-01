"""P2 Oracle inspection must be syntactically valid, redacted and read-only."""
from __future__ import annotations

import ast
import io
import subprocess
import textwrap
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/oracle-ssh-readonly-access.yml"


def remote_python() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "ssh -i " in source
    assert "'python3 -' <<'PY'" in source
    return textwrap.dedent(source.split("<<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0])


def test_readonly_workflow_has_no_mutating_commands():
    source = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("sudo ", "git fetch", "git reset", "systemctl restart",
                      "systemctl stop", "systemctl daemon-reload", "systemd-run",
                      "curl ", "pip install", "workflow_run:", "schedule:"):
        assert forbidden not in source
    ast.parse(remote_python())
    # A PR-branch push must run the read-only inspection without triggering
    # router-release-stage.yml (which is main-only and can write worktrees).
    assert 'branches: [main, fix/router-oracle-p2-readonly-20261101]' in source
    stage = (WORKFLOW.parent / 'router-release-stage.yml').read_text(encoding='utf-8')
    assert 'branches: [main]' in stage
    assert 'fix/router-oracle-p2-readonly-20261101' not in stage


def test_readonly_inspection_never_prints_exec_or_environment_secrets():
    secret = "SENSITIVE_SHOULD_NOT_APPEAR_4711"
    values = {
        "ActiveState": "active",
        "WorkingDirectory": "/home/ubuntu/Proyecto/Herramientas/Router",
        "ExecStart": (
            "argv[]=/usr/bin/python3 -m uvicorn router.api:app --host 127.0.0.1 "
            "--port 8010 --token " + secret + " ; ignore_errors=no"
        ),
        "DropInPaths": "/etc/systemd/system/router.service.d/zzzz-router-frozen-release.conf",
        "Environment": "ROUTER_OFFLINE_ONLY=1 SECRET=" + secret,
        "EnvironmentFiles": "/private/" + secret + ".env",
    }

    def fake_run(args, **kwargs):
        if args[:2] == ("systemctl", "show"):
            value = values.get(args[4], "")
            return subprocess.CompletedProcess(args, 0, value + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    output = io.StringIO()
    with patch("subprocess.run", side_effect=fake_run), redirect_stdout(output):
        exec(compile(remote_python(), "<oracle-readonly>", "exec"), {"__name__": "oracle_readonly_test"})
    report = output.getvalue()
    assert secret not in report
    assert "/private/" not in report
    assert "service_state=active" in report
    assert "exec_record=parsed" in report
    assert "offline_flag=explicit_on" in report
    assert "frozen_release_dropin=present" in report
    assert "inspection_complete=yes" in report
