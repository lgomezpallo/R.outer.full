"""End-to-end safety checks for the private provisional Oracle candidate."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECONCILE = ROOT / "scripts/oracle_reconcile.sh"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_conflicted_oracle(tmp_path: Path, *, overlay_passes: bool = True,
                           tracked_passes: bool = True,
                           with_collision: bool = False) -> Path:
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "router-test@example.invalid")
    git(repo, "config", "user.name", "Router Test")
    (repo / "router").mkdir()
    (repo / "tests").mkdir()
    router = repo / "router/router.py"
    router.write_text("class Router:\n    def route(self):\n        return 1\n")
    (repo / "settings.txt").write_text("base\n")
    expected_settings = "local" if tracked_passes else "base"
    (repo / "tests/test_candidate.py").write_text(
        "from pathlib import Path\n"
        "from router.router import Router\n"
        "def test_combined_candidate():\n"
        "    assert Router().route() == 3\n"
        f"    assert Path('settings.txt').read_text() == '{expected_settings}\\n'\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    router.write_text("class Router:\n    def route(self):\n        return 3\n")
    if with_collision:
        (repo / "router/collision.py").write_text("VALUE = 1\n")
    (repo / "scripts").mkdir()
    helper_names = ["oracle_provisional_candidate.py", "oracle_untracked_overlay.py"]
    if with_collision:
        helper_names.append("oracle_collision_probe.py")
    for name in helper_names:
        (repo / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)

    router.write_text("class Router:\n    def route(self):\n        return 2\n")
    if with_collision:
        (repo / "router/collision.py").write_text("VALUE = 2\n")
    (repo / "settings.txt").write_text("local\n")
    (repo / "router/local_only.py").write_text("VALUE = 7\n")
    (repo / "tests/test_local_only.py").write_text(
        "from router.local_only import VALUE\n"
        "def test_secret_private_name():\n"
        f"    assert VALUE == {7 if overlay_passes else 8}\n"
    )
    (repo / "private.env").write_text("PRIVATE_KEY=NEVER_EXPOSE\n")
    return repo


def run_reconcile(repo: Path, *, test_enabled: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "ROUTER_APP_DIR": str(repo),
        "ROUTER_UPSTREAM_REF": "origin/main",
        "ROUTER_RECONCILE_TESTS": "1" if test_enabled else "0",
    })
    return subprocess.run(
        ["bash", str(RECONCILE)], env=env, capture_output=True,
        text=True, check=False, timeout=120,
    )


@pytest.mark.parametrize("overlay_passes", [True, False])
def test_conflict_runs_private_provisional_tests_without_changing_oracle(
    tmp_path: Path, overlay_passes: bool,
) -> None:
    repo = make_conflicted_oracle(tmp_path, overlay_passes=overlay_passes)
    before_status = git(repo, "status", "--porcelain")
    before_worktrees = git(repo, "worktree", "list", "--porcelain")
    before_head = git(repo, "rev-parse", "HEAD")
    before_router = (repo / "router/router.py").read_bytes()
    result = run_reconcile(repo)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "reconcile_status=conflicts" in result.stdout
    assert "router_provisional=upstream_methods_unreviewed" in result.stdout
    assert "router_provisional_semantic_merge_approved=no" in result.stdout
    assert "router_provisional_deployment_approved=no" in result.stdout
    assert "router_provisional_baseline_tests=failed" in result.stdout
    assert "router_provisional_tracked_tests=passed" in result.stdout
    assert "router_provisional_overlay_copied=2" in result.stdout
    assert "router_provisional_overlay_tests=" + ("passed" if overlay_passes else "failed") in result.stdout
    assert "router_provisional_production_unchanged=yes" in result.stdout
    assert "test_secret_private_name" not in result.stdout + result.stderr
    assert "PRIVATE_KEY" not in result.stdout + result.stderr
    assert "NEVER_EXPOSE" not in result.stdout + result.stderr
    assert "local_only.py" not in result.stdout + result.stderr
    assert git(repo, "status", "--porcelain") == before_status
    assert git(repo, "worktree", "list", "--porcelain") == before_worktrees
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert (repo / "router/router.py").read_bytes() == before_router


def test_conflict_provisional_candidate_skips_tests_when_disabled(tmp_path: Path) -> None:
    repo = make_conflicted_oracle(tmp_path)
    before = git(repo, "status", "--porcelain")
    result = run_reconcile(repo, test_enabled=False)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "router_provisional=upstream_methods_unreviewed" in result.stdout
    assert "router_provisional_tracked_tests=skipped" in result.stdout
    assert "router_provisional_overlay_tests=" not in result.stdout
    assert git(repo, "status", "--porcelain") == before


def test_overlay_is_counted_even_when_other_oracle_tracked_edits_fail(tmp_path: Path) -> None:
    repo = make_conflicted_oracle(tmp_path, tracked_passes=False)
    before = git(repo, "status", "--porcelain")
    before_worktrees = git(repo, "worktree", "list", "--porcelain")
    result = run_reconcile(repo)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "router_provisional_baseline_tests=passed" in result.stdout
    assert "router_provisional_tracked_tests=failed" in result.stdout
    assert "router_provisional_overlay_copied=2" in result.stdout
    assert "router_provisional_overlay_tests=failed" in result.stdout
    assert "router_provisional_deployment_approved=no" in result.stdout
    assert "test_secret_private_name" not in result.stdout + result.stderr
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "worktree", "list", "--porcelain") == before_worktrees


def test_private_collision_alternate_runs_without_changing_oracle(tmp_path: Path) -> None:
    repo = make_conflicted_oracle(tmp_path, with_collision=True)
    before = git(repo, "status", "--porcelain")
    before_worktrees = git(repo, "worktree", "list", "--porcelain")
    before_local = (repo / "router/collision.py").read_bytes()
    result = run_reconcile(repo)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "router_provisional_collision_status=tested_private_variant" in result.stdout, [line for line in result.stdout.splitlines() if "collision_" in line]
    assert "router_provisional_collision_tests=passed" in result.stdout
    assert "router_provisional_semantic_merge_approved=no" in result.stdout
    assert "router_provisional_deployment_approved=no" in result.stdout
    assert "router/collision.py" not in result.stdout + result.stderr
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "worktree", "list", "--porcelain") == before_worktrees
    assert (repo / "router/collision.py").read_bytes() == before_local
