"""Stage 8 offline safety policy: no GitHub Actions job may contact Oracle.

This is a source-level CI regression guard, not an account billing verification.
The guard only takes effect on GitHub's default branch after authorized merge.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# Every workflow in this repository that can SSH to Oracle.
REMOTE_JOBS = {
    "oracle-deploy.yml": "deploy",
    "router-oracle-monitor.yml": "oracle-check",
    "router-release-stage.yml": "oracle-stage",
    "router-release-promote.yml": "oracle-promote",
    "router-release-live-smoke.yml": "oracle-live-smoke",
}


@pytest.mark.parametrize("filename,job", sorted(REMOTE_JOBS.items()))
def test_oracle_jobs_fail_closed_in_offline_freeze(filename, job):
    source = (WORKFLOWS / filename).read_text(encoding="utf-8")
    marker = f"  {job}:\n"
    assert source.count(marker) == 1, f"{filename}: missing or duplicate job"
    job_body = source.split(marker, 1)[1]
    job_header = job_body.split("    needs:", 1)[0]
    # Literal GitHub Actions expression. Do not replace with a user-controlled input.
    if filename == "router-release-stage.yml":
        approved = "    if: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
        assert approved in job_header, "Oracle staging is not pinned to the candidate branch"
        assert job_header.count("    if: ") == 1
    else:
        assert "    if: ${{ false }}" in job_header, f"{filename}: Oracle job is enabled"
    assert "ssh -i " in job_body, f"{filename}: review changed remote execution path"


@pytest.mark.parametrize("filename", sorted(REMOTE_JOBS))
def test_oracle_workflows_have_no_automatic_triggers(filename):
    source = (WORKFLOWS / filename).read_text(encoding="utf-8")
    assert source.count("\non:\n") == 1
    triggers = source.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "  workflow_dispatch:" in triggers
    if filename == "router-release-stage.yml":
        assert "  push:\n    branches: [main]\n    paths:\n      - .github/workflows/router-release-stage.yml\n" in triggers
    else:
        assert "  push:" not in triggers
    assert "  pull_request:" not in triggers
    assert "  schedule:" not in triggers
    assert "  workflow_run:" not in triggers


def test_pr_ci_remains_offline_and_cannot_ssh():
    source = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "  pull_request:" in source
    assert "      - run: pytest" in source
    assert "docker build --network none --pull=false --build-arg ROUTER_OFFLINE_WHEELS=1 -t router-ci ." in source
    assert "python -m pip install --no-index --find-links=wheelhouse" in source
    assert "ssh -i " not in source
    assert "workflow_dispatch:" not in source
