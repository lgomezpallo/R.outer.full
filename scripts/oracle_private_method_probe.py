#!/usr/bin/env python3
"""Privately test alternate Router method implementations in a detached worktree.

Never copy Oracle source into GitHub, logs, artifacts or the live checkout.
This is a diagnostic experiment, NOT a semantic merge or deployment approval.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

METHODS = ("_classify_failure", "route")
VARIANTS = (
    ("github", ()),
    ("oracle_classify", ("_classify_failure",)),
    ("oracle_route", ("route",)),
    ("oracle_both", METHODS),
)


def method_ranges(source: str) -> dict[str, tuple[int, int]]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Router":
            result = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                    result[item.name] = (start, item.end_lineno)
            return result
    raise ValueError("missing Router class")


def transplant(upstream: str, local: str, selected: tuple[str, ...]) -> str:
    """Replace only selected method bodies; leave all other upstream code intact."""
    upstream_ranges = method_ranges(upstream)
    local_ranges = method_ranges(local)
    lines = upstream.splitlines(keepends=True)
    source_lines = local.splitlines(keepends=True)
    edits = []
    for name in selected:
        if name not in upstream_ranges or name not in local_ranges:
            raise ValueError("method missing from one variant")
        start, end = upstream_ranges[name]
        local_start, local_end = local_ranges[name]
        original_indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        local_indent = len(source_lines[local_start - 1]) - len(source_lines[local_start - 1].lstrip())
        if original_indent != local_indent:
            raise ValueError("method indentation mismatch")
        edits.append((start, end, source_lines[local_start - 1:local_end]))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start - 1:end] = replacement
    result = "".join(lines)
    ast.parse(result)
    return result


def failing_test_ids(output: str, test_target: str) -> list[str]:
    """Extract public test function names only; never print assertion details."""
    pattern = re.compile("^FAILED " + re.escape(test_target) + "::([A-Za-z_][A-Za-z0-9_]*)")
    result: list[str] = []
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            test_id = test_target + "::" + match.group(1)
            if test_id not in result:
                result.append(test_id)
    return result[:20]


def git(app: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(app), *args], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def run_probe(app: Path, upstream: str, *, test_target: str = "tests/test_router.py") -> int:
    if not test_target.startswith("tests/") or not test_target.endswith(".py") or ".." in Path(test_target).parts:
        print("router_private_probe=invalid_test_target")
        return 2
    source_path = app / "router/router.py"
    try:
        local = source_path.read_text(encoding="utf-8")
        method_ranges(local)
    except (OSError, UnicodeError, SyntaxError, ValueError):
        print("router_private_probe=local_source_unavailable")
        return 2

    # Worktree is outside the live repository; no untracked local files copied.
    with tempfile.TemporaryDirectory(prefix="router-method-probe-") as temp:
        candidate = Path(temp) / "candidate"
        created = False
        try:
            result = git(app, "worktree", "add", "--detach", str(candidate), upstream)
            if result.returncode:
                print("router_private_probe=worktree_unavailable")
                return 2
            created = True
            target = candidate / "router/router.py"
            original = target.read_text(encoding="utf-8")
            try:
                for name in METHODS:
                    if name not in method_ranges(original) or name not in method_ranges(local):
                        raise ValueError("method missing")
            except (SyntaxError, ValueError):
                print("router_private_probe=methods_unavailable")
                return 2
            python = app / ".venv/bin/python"
            if not python.is_file():
                python = Path(sys.executable)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(candidate)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            for name, selected in VARIANTS:
                try:
                    target.write_text(transplant(original, local, selected), encoding="utf-8")
                    run = subprocess.run(
                        [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider", test_target],
                        cwd=candidate, env=env, capture_output=True, text=True,
                        timeout=75, check=False,
                    )
                    status = "passed" if run.returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    status = "timeout"
                except (OSError, SyntaxError, ValueError):
                    status = "unavailable"
                # Do not print pytest output: Oracle source or secrets could appear
                # in failures, assertions, tracebacks, and repr().
                print(f"router_private_probe_{name}={status}")
                if status == "failed":
                    # The public test names are safe; pytest's assertions,
                    # exception repr and private source remain undisclosed.
                    ids = failing_test_ids(run.stdout, test_target)
                    print(f"router_private_probe_{name}_failed_test_count={len(ids)}")
                    if ids:
                        print(f"router_private_probe_{name}_failed_tests={','.join(ids)}")
            print("router_private_probe=completed")
            print("router_private_probe_untracked_copied=no")
            print("router_private_probe_production_unchanged=yes")
            return 0
        finally:
            if created:
                git(app, "worktree", "remove", "--force", str(candidate))


def main() -> int:
    if len(sys.argv) != 3:
        print("router_private_probe=invalid_arguments")
        return 2
    app = Path(sys.argv[1]).resolve()
    upstream = sys.argv[2]
    # Only the caller's previously verified ref is accepted.
    if git(app, "rev-parse", "--verify", f"{upstream}^{{commit}}").returncode:
        print("router_private_probe=invalid_upstream")
        return 2
    return run_probe(app, upstream,
                     test_target=os.environ.get("ROUTER_PRIVATE_PROBE_TEST_TARGET", "tests/test_router.py"))


if __name__ == "__main__":
    raise SystemExit(main())
