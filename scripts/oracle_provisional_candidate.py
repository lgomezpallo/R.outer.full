#!/usr/bin/env python3
"""Privately test an upstream-method candidate when Oracle method merge is blocked.

This is a diagnostic only. Never approves a merge/deploy or changes Oracle.
No Oracle filenames, test names, tracebacks, source, or secrets go to stdout.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

CONFLICT_PATH = "router/router.py"
TEST_TIMEOUT_S = 180


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )


def unmerged_paths(repo: Path) -> set[str]:
    result = git(repo, "ls-files", "-u", "-z")
    if result.returncode:
        raise ValueError("unmerged index unavailable")
    paths: set[str] = set()
    for record in result.stdout.split(b"\0"):
        if record:
            if b"\t" not in record:
                raise ValueError("invalid index entry")
            paths.add(os.fsdecode(record.split(b"\t", 1)[1]))
    return paths


def safe_candidate(app: Path, candidate: Path, upstream: str) -> bool:
    if candidate == app or candidate.is_relative_to(app) or app.is_relative_to(candidate):
        return False
    if (candidate / CONFLICT_PATH).is_symlink():
        return False
    top = git(candidate, "rev-parse", "--show-toplevel")
    upstream_sha = git(app, "rev-parse", "--verify", upstream + "^{commit}")
    head = git(candidate, "rev-parse", "HEAD")
    return (
        top.returncode == 0 and Path(os.fsdecode(top.stdout.strip())).resolve() == candidate
        and upstream_sha.returncode == 0 and head.returncode == 0
        and upstream_sha.stdout.strip() == head.stdout.strip()
        and unmerged_paths(candidate) == {CONFLICT_PATH}
    )


def run_tests(python: Path, candidate: Path, env: dict[str, str]) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=candidate, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=TEST_TIMEOUT_S, check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(app: Path, upstream: str, candidate: Path, test_enabled: bool) -> int:
    app, candidate = app.resolve(), candidate.resolve()
    if not safe_candidate(app, candidate, upstream):
        print("router_provisional=blocked")
        return 3

    # Resolve ONLY the unmerged router/router.py in the disposable worktree,
    # choosing the upstream variant for diagnostic testing, not for production.
    restored = git(candidate, "restore", "--source=HEAD", "--staged", "--worktree",
                   "--", CONFLICT_PATH)
    if restored.returncode or unmerged_paths(candidate):
        print("router_provisional=blocked")
        return 3

    print("router_provisional=upstream_methods_unreviewed")
    print("router_provisional_semantic_merge_approved=no")
    print("router_provisional_deployment_approved=no")

    if not test_enabled:
        print("router_provisional_tracked_tests=skipped")
        print("router_provisional_production_unchanged=yes")
        return 0

    python = app / ".venv/bin/python"
    if not python.is_file():
        python = Path(sys.executable)

    with tempfile.TemporaryDirectory(prefix="router-provisional-private-") as temp:
        # Strip credentials, tokens and Oracle service environment from tests.
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": temp,
            "PYTHONPATH": str(candidate),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        # Run a clean upstream baseline with the same Python and environment.
        # If this fails too, the candidate failure cannot be attributed to
        # Oracle's tracked changes without additional evidence.
        baseline = Path(temp) / "upstream"
        if git(app, "worktree", "add", "--detach", str(baseline), upstream).returncode == 0:
            try:
                baseline_env = dict(env)
                baseline_env["PYTHONPATH"] = str(baseline)
                baseline_ok = run_tests(python, baseline, baseline_env)
                print("router_provisional_baseline_tests="
                      + ("passed" if baseline_ok else "failed"))
            finally:
                git(app, "worktree", "remove", "--force", str(baseline))
        else:
            print("router_provisional_baseline_tests=unavailable")

        tracked_ok = run_tests(python, candidate, env)
        print("router_provisional_tracked_tests=" + ("passed" if tracked_ok else "failed"))
        # Even a failing tracked candidate can be staged with safe local-only
        # sources to learn how many are eligible. It still cannot be deployed.
        untracked = git(app, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked.returncode == 0 and untracked.stdout:
        overlay_source = git(app, "show", upstream + ":scripts/oracle_untracked_overlay.py")
            if overlay_source.returncode == 0:
                overlay = Path(temp) / "overlay.py"
                overlay.write_bytes(overlay_source.stdout)
                staged = subprocess.run(
                    [sys.executable, str(overlay), str(app), str(candidate)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=env, timeout=60, check=False,
                )
                if staged.returncode == 0:
                    # Only sanitized integer counts, never private paths.
                    for key in ("total", "eligible", "copied", "blocked", "skipped"):
                        match = re.search(
                            rb"(?m)^untracked_overlay_" + key.encode() + rb"=([0-9]+)$",
                            staged.stdout,
                        )
                        if match:
                            print("router_provisional_overlay_" + key + "="
                                  + match.group(1).decode("ascii"))
                    copied = re.search(rb"(?m)^untracked_overlay_copied=([0-9]+)$",
                                       staged.stdout)
                    if copied and int(copied.group(1)) > 0:
                        overlay_ok = run_tests(python, candidate, env)
                        print("router_provisional_overlay_tests="
                              + ("passed" if overlay_ok else "failed"))
                    else:
                        print("router_provisional_overlay_tests=skipped")
                else:
                    print("router_provisional_overlay=blocked")
            else:
                print("router_provisional_overlay=unavailable")
        print("router_provisional_production_unchanged=yes")
    return 0


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[4] not in {"0", "1"}:
        print("router_provisional=invalid_arguments")
        return 2
    try:
        return run(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]),
                   sys.argv[4] == "1")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("router_provisional=blocked")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
