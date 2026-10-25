"""Regression tests for private three-way merging of Oracle Router methods."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/oracle_method_three_way.py"
spec = importlib.util.spec_from_file_location("oracle_method_three_way", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


BASE = (
    "class Router:\n"
    "    def _classify_failure(self, exc):\n"
    "        a = 1\n"
    "        b = 2\n"
    "        c = 3\n"
    "        d = 4\n"
    "        e = 5\n"
    "        f = 6\n"
    "        g = 7\n"
    "        h = 8\n"
    "        i = 9\n"
    "        j = 10\n"
    "        k = 11\n"
    "        return a + b + c + d + e + f + g + h + i + j + k\n"
    "    def route(self):\n"
    "        return 'original'\n"
)


def test_disjoint_edits_in_same_method_merge_without_discarding_either():
    upstream = BASE.replace("k = 11", "k = 22")
    local = BASE.replace("a = 1", "a = 33")
    merged, overlaps = module.merge_sources(BASE, upstream, local)
    assert overlaps == 1
    assert "a = 33" in merged
    assert "k = 22" in merged
    assert "a = 1\n" not in merged


def test_local_only_method_change_preserved_with_upstream_new_method():
    upstream = BASE + "    def new_upstream_method(self):\n        return 'new'\n"
    local = BASE.replace("return 'original'", "return 'local'")
    merged, overlaps = module.merge_sources(BASE, upstream, local)
    assert overlaps == 0
    assert "return 'local'" in merged
    assert "new_upstream_method" in merged


def test_conflicting_same_line_refuses_automatic_merge():
    upstream = BASE.replace("a = 1", "a = 22")
    local = BASE.replace("a = 1", "a = 33")
    with pytest.raises(module.MergeBlocked):
        module.merge_sources(BASE, upstream, local)


def test_local_import_or_added_method_is_never_silently_discarded():
    with pytest.raises(module.MergeBlocked):
        module.merge_sources(BASE, BASE, "import os\n" + BASE)
    with pytest.raises(module.MergeBlocked):
        module.merge_sources(BASE, BASE, BASE + "    def oracle_only(self):\n        return 99\n")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_cli_writes_only_candidate_and_sanitizes_output(tmp_path: Path):
    repo = tmp_path / "oracle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "Router Test")
    git(repo, "config", "user.email", "router-test@example.invalid")
    (repo / "router").mkdir()
    source = repo / "router/router.py"
    source.write_text(BASE)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    source.write_text(BASE.replace("k = 11", "k = 22"))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "reset", "--hard", base)
    source.write_text(BASE.replace("a = 1", "a = 33  # PRIVATE_ORACLE_SECRET"))
    original_status = git(repo, "status", "--porcelain")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    target = candidate / "router.py"
    target.write_text("not yet merged\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), base, "origin/main", str(target)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "router_method_merge=clean" in result.stdout
    assert "PRIVATE_ORACLE_SECRET" not in result.stdout + result.stderr
    assert "k = 22" in target.read_text()
    assert "a = 33" in target.read_text()
    assert git(repo, "status", "--porcelain") == original_status
    assert "PRIVATE_ORACLE_SECRET" in source.read_text()
    blocked = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), base, "origin/main", str(source)],
        capture_output=True, text=True, check=False,
    )
    assert blocked.returncode == 3
    assert "router_method_merge=blocked" in blocked.stdout
    assert git(repo, "status", "--porcelain") == original_status
