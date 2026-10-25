"""Safety and conflict tests for the isolated Oracle reconciliation candidate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oracle_reconcile.sh"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, text=True, capture_output=True,
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path, upstream_change: str | None) -> Path:
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "router-test@example.invalid")
    git(repo, "config", "user.name", "Router Test")
    (repo / "router").mkdir()
    (repo / "router" / "router.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    if upstream_change is None:
        (repo / "README.md").write_text("upstream change\n")
    else:
        (repo / "router" / "router.py").write_text(upstream_change)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)
    (repo / "router" / "router.py").write_text("value = 2\n")
    return repo


def reconcile(repo: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "ROUTER_APP_DIR": str(repo),
        "ROUTER_RECONCILE_TESTS": "0",
        "ROUTER_UPSTREAM_REF": "origin/main",
    })
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env, text=True, capture_output=True, check=False,
    )


@pytest.mark.parametrize(
    ("upstream_change", "expected_status"),
    [
        (None, "candidate_applied"),
        ("value = 3\n", "conflicts"),
    ],
)
def test_reconcile_is_private_and_does_not_modify_oracle(
    tmp_path: Path, upstream_change: str | None, expected_status: str,
) -> None:
    repo = make_repo(tmp_path, upstream_change)
    original_head = git(repo, "rev-parse", "HEAD")
    original_status = git(repo, "status", "--porcelain")
    original_content = (repo / "router" / "router.py").read_text()
    original_worktrees = git(repo, "worktree", "list", "--porcelain")
    result = reconcile(repo)

    assert result.returncode == 0, result.stderr
    assert f"reconcile_status={expected_status}" in result.stdout
    assert "production_checkout_unchanged=yes" in result.stdout
    assert git(repo, "rev-parse", "HEAD") == original_head
    assert git(repo, "status", "--porcelain") == original_status
    assert git(repo, "worktree", "list", "--porcelain") == original_worktrees
    assert (repo / "router" / "router.py").read_text() == original_content
    assert "value = 2" not in result.stdout
    if expected_status == "conflicts":
        assert "router/router.py" in result.stdout
    else:
        assert "candidate_tests=skipped" in result.stdout
