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
    (app / "router/__init__.py").write_text("")
    (app / "router/base.py").write_text("VALUE = 1\n")
    (app / "tests").mkdir()
    (app / "tests/test_base.py").write_text("def test_base():\n    assert True\n")
    git(app, "add", ".")
    git(app, "commit", "-qm", "base")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "router").mkdir()
    (candidate / "router/__init__.py").write_text("")
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


def test_failing_local_overlay_never_prints_private_test_identifiers(tmp_path: Path):
    app, _ = make_repo(tmp_path)
    base = git(app, "rev-parse", "HEAD")
    (app / "scripts").mkdir()
    (app / "scripts/oracle_untracked_overlay.py").write_text(SCRIPT.read_text())
    (app / "README.md").write_text("upstream\n")
    git(app, "add", ".")
    git(app, "commit", "-qm", "upstream with overlay")
    git(app, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(app, "reset", "--hard", base)
    (app / "tests/test_oracle_only.py").write_text(
        "def test_private_internal_customer_name():\n"
        "    assert False, 'NEVER_LOG_PRIVATE_ASSERTION'\n"
    )
    before = git(app, "status", "--porcelain")
    env = os.environ.copy()
    env.update({"ROUTER_APP_DIR": str(app), "ROUTER_UPSTREAM_REF": "origin/main",
                "ROUTER_RECONCILE_TESTS": "1", "ROUTER_PYTHON": sys.executable})
    result = subprocess.run(["bash", str(RECONCILE)], env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 5, result.stdout + result.stderr
    assert "candidate_tests=passed" in result.stdout
    assert "untracked_overlay_tests=failed" in result.stdout
    assert "test_oracle_only" not in result.stdout + result.stderr
    assert "test_private_internal_customer_name" not in result.stdout + result.stderr
    assert "NEVER_LOG_PRIVATE_ASSERTION" not in result.stdout + result.stderr
    assert git(app, "status", "--porcelain") == before


def test_private_reason_counts_explain_skips_and_blocks_without_paths(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    (app / "router/collision.py").write_text("ORACLE_PRIVATE_CONTENT = 1\n")
    (candidate / "router/collision.py").write_text("UPSTREAM = 1\n")
    (app / "router/oversize.py").write_bytes(b"x" * 256_001)
    (app / "router/link.py").symlink_to("/etc/passwd")
    (app / "scripts").mkdir()
    (app / "scripts/legacy.py").write_text("PRIVATE_LEGACY = 1\n")
    (app / "scripts/old.sh").write_text("PRIVATE_SCRIPT = 1\n")
    (app / "docs").mkdir()
    (app / "docs/notes.txt").write_text("PRIVATE_NOTES = 1\n")
    (app / ".env").write_text("PRIVATE_TOKEN = 1\n")

    before = git(app, "status", "--porcelain")
    result = run_overlay(app, candidate)
    assert result.returncode == 0, result.stderr
    counts = {}
    for line in result.stdout.splitlines():
        if line.startswith("untracked_overlay_") and "=" in line:
            key, value = line.split("=", 1)
            if value.isdigit():
                counts[key.removeprefix("untracked_overlay_")] = int(value)
    assert counts["total"] == 7
    assert counts["eligible"] == 3
    assert counts["blocked"] == 3
    assert counts["copied"] == 0
    assert counts["skipped"] == 4
    for key in ("blocked_collision", "blocked_oversize", "blocked_symlink",
                "skipped_python_outside_roots", "skipped_other_source_like",
                "skipped_other", "skipped_unsafe_path"):
        assert counts[key] == 1
    assert sum(counts[k] for k in (
        "blocked_collision", "blocked_oversize", "blocked_symlink",
        "blocked_unsafe_layout", "blocked_nonregular", "blocked_io")) == counts["blocked"]
    assert sum(counts[k] for k in (
        "skipped_python_outside_roots", "skipped_other_source_like",
        "skipped_other", "skipped_unsafe_path", "skipped_sensitive_path")) == counts["skipped"]
    assert "ORACLE_PRIVATE_CONTENT" not in result.stdout + result.stderr
    assert "PRIVATE_TOKEN" not in result.stdout + result.stderr
    for private_name in ("collision.py", "oversize.py", "link.py", "legacy.py", "old.sh", ".env"):
        assert private_name not in result.stdout + result.stderr
    assert (candidate / "router/collision.py").read_text() == "UPSTREAM = 1\n"
    assert git(app, "status", "--porcelain") == before


def test_outside_python_roles_are_aggregated_and_sensitive_paths_are_never_copied(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    for root, filename in (("scripts", "helper.py"), ("tools", "adapter.py"),
                           ("applications", "entry.py")):
        (app / root).mkdir()
        (app / root / filename).write_text("ORACLE_ONLY_INTERNAL = 1\n")
    (app / "router/api_key.py").write_text("DO_NOT_EXPOSE = 1\n")
    (app / "tests/test_private.py").write_text("DO_NOT_EXPOSE = 1\n")
    before = git(app, "status", "--porcelain")
    result = run_overlay(app, candidate)
    assert result.returncode == 0
    assert "untracked_overlay_total=5" in result.stdout
    assert "untracked_overlay_skipped_python_outside_roots=3" in result.stdout
    assert "untracked_overlay_outside_python_scripts=1" in result.stdout
    assert "untracked_overlay_outside_python_tools=1" in result.stdout
    assert "untracked_overlay_outside_python_other=1" in result.stdout
    assert "untracked_overlay_skipped_sensitive_path=2" in result.stdout
    assert "untracked_overlay_copied=0" in result.stdout
    assert "ORACLE_ONLY_INTERNAL" not in result.stdout
    assert "DO_NOT_EXPOSE" not in result.stdout
    for name in ("helper.py", "adapter.py", "entry.py", "api_key.py", "test_private.py"):
        assert name not in result.stdout
    assert not (candidate / "router/api_key.py").exists()
    assert not (candidate / "tests/test_private.py").exists()
    assert git(app, "status", "--porcelain") == before


def test_outside_python_ast_audit_is_private_nonexecuting_and_read_only(tmp_path: Path):
    app, candidate = make_repo(tmp_path)
    for root in ("apps", "old_tests", "archive", "large", "backup", "legacy"):
        (app / root).mkdir()
    (app / "apps/module.py").write_text(
        "import router.api\\n"
        "class Internal:\\n    def work(self):\\n        return 1\\n"
        "if __name__ == '__main__':\\n    print('PRIVATE_MAIN_VALUE')\\n"
    )
    (app / "old_tests/check.py").write_text(
        "def test_private_customer():\\n    assert True\\n"
    )
    (app / "archive/bad.py").write_text("def invalid(:\\n")
    (app / "large/huge.py").write_bytes(b"x" * 256_001)
    (app / "backup/link.py").symlink_to("/etc/passwd")
    (app / "legacy/runner.py").write_text(
        "def work():\\n    return 2\\nwork()\\n"
    )
    (app / "apps/api_token.py").write_text("NEVER_AUDIT = 1\\n")
    before = git(app, "status", "--porcelain")
    result = run_overlay(app, candidate)
    assert result.returncode == 0, result.stderr
    assert "untracked_overlay_skipped_python_outside_roots=6" in result.stdout
    assert "untracked_overlay_skipped_sensitive_path=1" in result.stdout
    for key, value in (
        ("valid", 3), ("syntax_error", 1), ("unsafe", 1),
        ("oversize", 1), ("io", 0), ("router_imports", 1),
        ("tests", 1), ("main_guard", 1), ("top_level_calls", 1),
        ("declarations_only", 2),
    ):
        assert f"untracked_overlay_outside_python_audit_{key}={value}" in result.stdout
    for secret in ("PRIVATE_MAIN_VALUE", "test_private_customer", "NEVER_AUDIT",
                   "module.py", "runner.py", "link.py", "api_token.py"):
        assert secret not in result.stdout + result.stderr
    assert not (candidate / "apps").exists()
    assert git(app, "status", "--porcelain") == before
