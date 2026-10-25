"""Private, non-deploying staging of Oracle-only Python sources."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/oracle_untracked_overlay.py"
RECONCILE = SCRIPT.with_name("oracle_reconcile.sh")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "oracle"
    app.mkdir()
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    git(app, "config", "user.name", "Router Test")
    git(app, "config", "user.email", "router@example.invalid")
    (app / "router").mkdir()
    (app / "router/base.py").write_text("VALUE = 1\n")
    (app / "tests").mkdir()
    (app / "tests/test_base.py").write_text("def test_base():\n    assert True\n")
    git(app, "add", ".")
    git(app, "commit", "-qm", "base")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "router").mkdir()
    (candidate / "router/base.py").write_text("VALUE = 1\n")
    return app, candidate


def run_overlay(app: Path, candidate: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), str(app), str(candidate)],
                          capture_output=True, text=True, check=False)


def test_only_allowlisted_python_sources_are_staged_without_logging_contents(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    (app / "router/local.py").write_text("LOCAL_SECRET_NOT_FOR_LOGS = 1\n")
    (app / "tests/test_local.py").write_text("def test_local():\n    assert True\n")
    (app / "router/private.json").write_text('{"api_key":"DO_NOT_PRINT"}')
    (app / ".env").write_text("TOKEN=DO_NOT_PRINT")
    (app / "scripts").mkdir()
    (app / "scripts/tool.py").write_text("PRIVATE_TOOL=1\n")
    before = git(app, "status", "--porcelain")
    result = run_overlay(app, candidate)
    assert result.returncode == 0, result.stderr
    assert "untracked_overlay_total=5" in result.stdout
    assert "untracked_overlay_copied=2" in result.stdout
    assert "untracked_overlay_skipped=3" in result.stdout
    assert "LOCAL_SECRET_NOT_FOR_LOGS" not in result.stdout + result.stderr
    assert "DO_NOT_PRINT" not in result.stdout + result.stderr
    assert (candidate / "router/local.py").exists()
    assert (candidate / "tests/test_local.py").exists()
    assert not (candidate / ".env").exists()
    assert not (candidate / "router/private.json").exists()
    assert not (candidate / "scripts/tool.py").exists()
    assert git(app, "status", "--porcelain") == before


def test_does_not_overwrite_existing_upstream_path(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    (app / "router/local.py").write_text("ORACLE_ONLY=1\n")
    (candidate / "router/local.py").write_text("UPSTREAM=1\n")
    result = run_overlay(app, candidate)
    assert result.returncode == 0
    assert "untracked_overlay_blocked=1" in result.stdout
    assert "untracked_overlay_copied=0" in result.stdout
    assert (candidate / "router/local.py").read_text() == "UPSTREAM=1\n"


def test_rejects_symlinks_and_candidate_inside_live_tree(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    (app / "router/link.py").symlink_to("/etc/passwd")
    result = run_overlay(app, candidate)
    assert result.returncode == 0
    assert "untracked_overlay_blocked=1" in result.stdout
    assert not (candidate / "router/link.py").exists()
    unsafe = run_overlay(app, app / "router")
    assert unsafe.returncode == 3
    assert "untracked_overlay=blocked" in unsafe.stdout


def test_full_reconcile_tests_private_untracked_overlay_and_still_blocks_release(tmp_path: Path):
    app, _ = make_repo(tmp_path)
    base = git(app, "rev-parse", "HEAD")
    (app / "scripts").mkdir()
    (app / "scripts/oracle_untracked_overlay.py").write_text(SCRIPT.read_text())
    (app / "README.md").write_text("GitHub upstream\n")
    git(app, "add", ".")
    git(app, "commit", "-qm", "upstream with private overlay helper")
    git(app, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(app, "reset", "--hard", base)
    (app / "router/local.py").write_text("VALUE = 42\n")
    (app / "tests/test_local.py").write_text(
        "from router.local import VALUE\n"
        "def test_local():\n    assert VALUE == 42\n"
    )
    (app / ".env").write_text("DO_NOT_LOG_THIS_TOKEN=abc\n")
    before = git(app, "status", "--porcelain")
    before_worktrees = git(app, "worktree", "list", "--porcelain")
    env = os.environ.copy()
    env.update({"ROUTER_APP_DIR": str(app), "ROUTER_UPSTREAM_REF": "origin/main",
                "ROUTER_RECONCILE_TESTS": "1", "ROUTER_PYTHON": sys.executable})
    result = subprocess.run(["bash", str(RECONCILE)], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 5, result.stdout + result.stderr
    assert "candidate_tests=passed" in result.stdout
    assert "untracked_overlay_copied=2" in result.stdout
    assert "untracked_overlay_tests=passed" in result.stdout
    assert "reconcile_status=untracked_files_require_review" in result.stdout
    assert "DO_NOT_LOG_THIS_TOKEN" not in result.stdout + result.stderr
    assert git(app, "status", "--porcelain") == before
    assert git(app, "worktree", "list", "--porcelain") == before_worktrees
    assert git(app, "rev-parse", "HEAD") == base
