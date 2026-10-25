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
    (repo / "tests").mkdir()
    (repo / "tests" / "test_candidate.py").write_text("def test_candidate():\n    assert True\n")
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

    assert result.returncode == (3 if expected_status == "conflicts" else 0), result.stderr
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


def test_candidate_tests_run_in_isolated_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change=None)
    env = os.environ.copy()
    env.update({
        "ROUTER_APP_DIR": str(repo),
        "ROUTER_RECONCILE_TESTS": "1",
        "ROUTER_UPSTREAM_REF": "origin/main",
    })
    before = git(repo, "status", "--porcelain")
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "reconcile_status=candidate_applied" in result.stdout
    assert "candidate_tests=passed" in result.stdout
    assert git(repo, "status", "--porcelain") == before
    assert len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_reconcile_includes_oracle_only_commits_in_candidate(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change=None)
    # Oracle HEAD may diverge from GitHub even with a clean working tree.
    git(repo, "add", "router/router.py")
    git(repo, "commit", "-qm", "oracle-only feature")
    original_head = git(repo, "rev-parse", "HEAD")
    original_worktrees = git(repo, "worktree", "list", "--porcelain")
    # Executed inside the candidate: catches lost committed Oracle changes.
    (repo / "tests" / "test_candidate.py").write_text(
        "from pathlib import Path\n"
        "def test_preserved_local_commit():\n"
        "    assert Path('router/router.py').read_text() == 'value = 2\\n'\n"
    )
    env = os.environ.copy()
    env.update({
        "ROUTER_APP_DIR": str(repo),
        "ROUTER_RECONCILE_TESTS": "1",
        "ROUTER_UPSTREAM_REF": "origin/main",
    })
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "local_unique_commits=1" in result.stdout
    assert "reconcile_status=candidate_applied" in result.stdout
    assert "candidate_tests=passed" in result.stdout
    assert git(repo, "rev-parse", "HEAD") == original_head
    assert git(repo, "worktree", "list", "--porcelain") == original_worktrees
    assert (repo / "router" / "router.py").read_text() == "value = 2\n"


def test_reconcile_reports_conflict_with_oracle_only_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change="value = 3\n")
    git(repo, "add", "router/router.py")
    git(repo, "commit", "-qm", "oracle-only conflicting feature")
    result = reconcile(repo)
    assert result.returncode == 3, result.stderr
    assert "local_unique_commits=1" in result.stdout
    assert "reconcile_status=conflicts" in result.stdout
    assert "router/router.py" in result.stdout


def test_untracked_oracle_files_block_false_success_without_exposing_contents(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change=None)
    secret = repo / "local-only-provider.py"
    secret.write_text("PRIVATE_LOCAL_VALUE=do_not_publish\\n")
    before_head = git(repo, "rev-parse", "HEAD")
    before_worktrees = git(repo, "worktree", "list", "--porcelain")
    result = reconcile(repo)
    assert result.returncode == 5, result.stderr
    assert "untracked_files_not_included=1" in result.stdout
    assert "reconcile_status=untracked_files_require_review" in result.stdout
    assert "production_checkout_unchanged=yes" in result.stdout
    assert "PRIVATE_LOCAL_VALUE" not in result.stdout
    assert secret.read_text() == "PRIVATE_LOCAL_VALUE=do_not_publish\\n"
    assert git(repo, "rev-parse", "HEAD") == before_head
    assert git(repo, "worktree", "list", "--porcelain") == before_worktrees


def test_conflict_is_not_masked_by_untracked_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change="value = 3\n")
    secret = repo / "secret-local.env"
    secret.write_text("NEVER_PRINT_THIS_SECRET\n")
    before = git(repo, "status", "--porcelain")
    result = reconcile(repo)
    assert result.returncode == 3, result.stderr + result.stdout
    assert "reconcile_status=conflicts" in result.stdout
    assert "router/router.py" in result.stdout
    assert "untracked_files_not_included=1" in result.stdout
    assert "untracked_other_count=1" in result.stdout
    assert "NEVER_PRINT_THIS_SECRET" not in result.stdout
    assert git(repo, "status", "--porcelain") == before


def test_private_symbol_diagnostics_identify_overlapping_methods(tmp_path: Path) -> None:
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "router-test@example.invalid")
    git(repo, "config", "user.name", "Router Test")
    (repo / "router").mkdir()
    path = repo / "router" / "router.py"
    path.write_text("class Router:\n    def route(self):\n        return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    # The diagnostic helper exists in upstream, but not in the live checkout.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "oracle_reconcile_symbols.py").write_text(
        (SCRIPT.parent / "oracle_reconcile_symbols.py").read_text()
    )
    path.write_text("class Router:\n    def route(self):\n        return 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)
    path.write_text(
        "class Router:\n"
        "    def route(self):\n"
        "        return 2\n"
        "    def add_provider(self):\n"
        "        return 'SECRET_NOT_FOR_PUBLIC_LOGS'\n"
    )
    before = path.read_text()
    result = reconcile(repo)
    assert result.returncode == 3, result.stderr + result.stdout
    assert "router_symbol_diagnostics=available" in result.stdout
    assert "router_overlapping_methods=route" in result.stdout
    assert "router_local_only_modified_methods=add_provider" in result.stdout
    assert "SECRET_NOT_FOR_PUBLIC_LOGS" not in result.stdout
    assert path.read_text() == before



def test_private_method_merge_refuses_same_line_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.email", "router-test@example.invalid")
    git(repo, "config", "user.name", "Router Test")
    (repo / "router").mkdir()
    path = repo / "router/router.py"
    path.write_text(
        "class Router:\n"
        "    def _classify_failure(self, exc):\n"
        "        return 1\n"
        "    def route(self):\n"
        "        return 1\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "scripts").mkdir()
    (repo / "scripts/oracle_method_three_way.py").write_text(
        (SCRIPT.parent / "oracle_method_three_way.py").read_text()
    )
    path.write_text(path.read_text().replace("return 1\n    def route", "return 3\n    def route"))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)
    path.write_text(path.read_text().replace("return 1\n    def route", "return 2\n    def route"))
    before = path.read_text()
    result = reconcile(repo)
    assert result.returncode == 3, result.stderr + result.stdout
    assert "router_method_merge=blocked" in result.stdout
    assert "reconcile_status=conflicts" in result.stdout
    assert path.read_text() == before


def test_untracked_files_do_not_prevent_tracked_candidate_tests(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upstream_change=None)
    (repo / "local-only.py").write_text("PRIVATE=secret\n")
    before = git(repo, "status", "--porcelain")
    env = os.environ.copy()
    env.update({
        "ROUTER_APP_DIR": str(repo),
        "ROUTER_RECONCILE_TESTS": "1",
        "ROUTER_UPSTREAM_REF": "origin/main",
    })
    result = subprocess.run(["bash", str(SCRIPT)], env=env,
                            text=True, capture_output=True, check=False)
    assert result.returncode == 5, result.stderr + result.stdout
    assert "candidate_tests=passed" in result.stdout
    assert "reconcile_status=untracked_files_require_review" in result.stdout
    assert "secret" not in result.stdout + result.stderr
    assert git(repo, "status", "--porcelain") == before
