"""Verify Oracle's private method comparison never alters the live checkout."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/oracle_private_method_probe.py"
spec = importlib.util.spec_from_file_location("oracle_private_method_probe", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          text=True, capture_output=True, check=True).stdout.strip()


def test_transplant_replaces_only_named_method():
    upstream = (
        "class Router:\n"
        "    def _classify_failure(self):\n"
        "        return 10\n"
        "    def route(self):\n"
        "        return 30\n"
        "    def untouched(self):\n"
        "        return 99\n"
    )
    oracle = (
        "class Router:\n"
        "    def route(self):\n"
        "        return 2\n"
        "    def _classify_failure(self):\n"
        "        return 20\n"
        "    def untouched(self):\n"
        "        return -1\n"
    )
    result = module.transplant(upstream, oracle, ("route",))
    assert "return 2" in result
    assert "return 10" in result
    assert "return 99" in result
    assert "return -1" not in result
    assert module.transplant(upstream, oracle, ()) == upstream


def test_failing_test_ids_never_include_assertion_values_or_other_files():
    output = (
        "FAILED tests/test_router.py::test_timeout - SECRET_ASSERTION_VALUE\n"
        "FAILED tests/test_router.py::test_timeout - DUPLICATE\n"
        "FAILED tests/test_router.py::test_auth[private-param] - SECRET_PARAM\n"
        "FAILED tests/test_other.py::test_private - PRIVATE_OTHER\n"
        "E  SECRET_TRACEBACK\n"
    )
    ids = module.failing_test_ids(output, "tests/test_router.py")
    assert ids == [
        "tests/test_router.py::test_timeout",
        "tests/test_router.py::test_auth",
    ]
    assert "SECRET" not in str(ids)
    assert "private-param" not in str(ids)


def test_private_probe_uses_detached_worktree_and_sanitizes_failures(tmp_path: Path):
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "Router Test")
    git(repo, "config", "user.email", "router-test@example.invalid")
    (repo / "router").mkdir()
    router_file = repo / "router/router.py"
    router_file.write_text(
        "class Router:\n"
        "    def _classify_failure(self):\n"
        "        return 1\n"
        "    def route(self):\n"
        "        return 1\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests/test_router.py").write_text(
        "from router.router import Router\n"
        "def test_expected_upstream_behavior():\n"
        "    assert Router().route() == 3, 'PRIVATE_FAILURE_NEVER_LOG'\n"
        "    assert Router()._classify_failure() == 10\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    router_file.write_text(
        "class Router:\n"
        "    def _classify_failure(self):\n"
        "        return 10\n"
        "    def route(self):\n"
        "        return 3\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)
    router_file.write_text(
        "class Router:\n"
        "    def _classify_failure(self):\n"
        "        return 20  # PRIVATE_LOCAL_VALUE\n"
        "    def route(self):\n"
        "        return 2\n"
    )
    untracked = repo / "router/private-provider.py"
    untracked.write_text("SECRET=PRIVATE_LOCAL_VALUE\n")
    original_head = git(repo, "rev-parse", "HEAD")
    original_status = git(repo, "status", "--porcelain")
    original_worktrees = git(repo, "worktree", "list", "--porcelain")
    original_source = router_file.read_text()

    env = os.environ.copy()
    env["ROUTER_PRIVATE_PROBE_TEST_TARGET"] = "tests/test_router.py"
    run = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), "origin/main"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    assert "router_private_probe_github=passed" in run.stdout
    assert "router_private_probe_oracle_classify=failed" in run.stdout
    assert "router_private_probe_oracle_route=failed" in run.stdout
    assert "router_private_probe_oracle_both=failed" in run.stdout
    assert "router_private_probe_oracle_classify_failed_test_count=1" in run.stdout
    assert "router_private_probe_oracle_route_failed_test_count=1" in run.stdout
    assert "router_private_probe_oracle_both_failed_tests=tests/test_router.py::test_expected_upstream_behavior" in run.stdout
    assert "router_private_probe_untracked_copied=no" in run.stdout
    assert "PRIVATE_LOCAL_VALUE" not in run.stdout + run.stderr
    assert "PRIVATE_FAILURE_NEVER_LOG" not in run.stdout + run.stderr
    assert git(repo, "rev-parse", "HEAD") == original_head
    assert git(repo, "status", "--porcelain") == original_status
    assert git(repo, "worktree", "list", "--porcelain") == original_worktrees
    assert router_file.read_text() == original_source
    assert untracked.read_text() == "SECRET=PRIVATE_LOCAL_VALUE\n"
