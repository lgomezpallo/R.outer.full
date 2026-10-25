#!/usr/bin/env python3
"""Privately stage a narrow, safe subset of Oracle-only source into a test worktree.

Never modifies the live checkout, never prints filenames or file contents,
never stages credentials/configuration, and never overwrites upstream files.
The result is only a diagnostic candidate, NOT a deployable release.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys

MAX_SOURCE_BYTES = 256_000
ALLOWED_ROOTS = frozenset({"router", "tests"})
SENSITIVE_COMPONENT = re.compile(r"(?:secret|credential|token|password|api[_-]?key|private)", re.I)


def untracked_paths(app: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(app), "ls-files", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    )
    return [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]


def is_safe_source_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and len(path.parts) >= 2
        and path.parts[0] in ALLOWED_ROOTS
        and path.suffix == ".py"
        and all(part not in {".", ".."} and not part.startswith(".") for part in path.parts)
        and not any(part in {"__pycache__", "venv", "node_modules", "secrets"} or SENSITIVE_COMPONENT.search(part) for part in path.parts)
    )


# Only aggregate counters are reported. Never emit Oracle paths, names or data.
# This list is intentionally narrow: configuration formats are not executable
# overlay candidates even if they may contain useful source/configuration.
SOURCE_LIKE_SUFFIXES = frozenset({".py", ".sh", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".yaml", ".yml", ".toml"})


def _skip_reason(name: str) -> str | None:
    path = PurePosixPath(name)
    if (path.is_absolute() or len(path.parts) < 2
            or any(part in {".", ".."} or part.startswith(".") for part in path.parts)
            or any(part in {"__pycache__", "venv", "node_modules", "secrets"} for part in path.parts)):
        return "skipped_unsafe_path"
    if any(SENSITIVE_COMPONENT.search(part) for part in path.parts):
        return "skipped_sensitive_path"
    if is_safe_source_path(name):
        return None
    if path.suffix == ".py":
        return "skipped_python_outside_roots"
    if path.suffix in SOURCE_LIKE_SUFFIXES:
        return "skipped_other_source_like"
    return "skipped_other"


# This audit never executes Oracle source or publishes its identifiers.
# AST flags are heuristics, not evidence that a file is safe to merge/deploy.
OUTSIDE_AUDIT_KEYS = (
    "outside_python_audit_valid", "outside_python_audit_syntax_error",
    "outside_python_audit_unsafe", "outside_python_audit_oversize",
    "outside_python_audit_io", "outside_python_audit_router_imports",
    "outside_python_audit_tests", "outside_python_audit_main_guard",
    "outside_python_audit_top_level_calls",
    "outside_python_audit_declarations_only",
)


def _audit_outside_python(app: Path, name: str) -> dict[str, int]:
    counts = {key: 0 for key in OUTSIDE_AUDIT_KEYS}
    source = app / name
    try:
        if (any(part.is_symlink() for part in (source, *source.parents) if part != app)
                or not source.resolve().is_relative_to(app)):
            counts["outside_python_audit_unsafe"] = 1
            return counts
        info = source.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            counts["outside_python_audit_unsafe"] = 1
            return counts
        if info.st_size > MAX_SOURCE_BYTES:
            counts["outside_python_audit_oversize"] = 1
            return counts
        data = source.read_bytes()
        if len(data) > MAX_SOURCE_BYTES:
            counts["outside_python_audit_oversize"] = 1
            return counts
    except (OSError, ValueError):
        counts["outside_python_audit_io"] = 1
        return counts
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, UnicodeError, ValueError):
        counts["outside_python_audit_syntax_error"] = 1
        return counts

    counts["outside_python_audit_valid"] = 1
    nodes = list(ast.walk(tree))
    counts["outside_python_audit_router_imports"] = int(any(
        (isinstance(node, ast.Import)
         and any(alias.name == "router" or alias.name.startswith("router.")
                 for alias in node.names))
        or (isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".", 1)[0] == "router")
        for node in nodes
    ))
    counts["outside_python_audit_tests"] = int(any(
        (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name.startswith("test_"))
        or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        for node in nodes
    ))
    counts["outside_python_audit_main_guard"] = int(any(
        isinstance(node, ast.If)
        and any(isinstance(child, ast.Name) and child.id == "__name__"
                for child in ast.walk(node.test))
        for node in tree.body
    ))
    top_level_calls = any(
        isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(isinstance(child, ast.Call) for child in ast.walk(node))
        for node in tree.body
    )
    counts["outside_python_audit_top_level_calls"] = int(top_level_calls)
    counts["outside_python_audit_declarations_only"] = int(
        not top_level_calls and any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            for node in tree.body
        )
    )
    return counts


def stage(app: Path, candidate: Path) -> dict[str, int]:
    app, candidate = app.resolve(), candidate.resolve()
    if (not app.is_dir() or not candidate.is_dir()
            or candidate.is_relative_to(app) or app.is_relative_to(candidate)):
        raise ValueError("unsafe worktree layout")

    reasons = (
        "skipped_unsafe_path", "skipped_sensitive_path", "skipped_python_outside_roots",
        "skipped_other_source_like", "skipped_other",
        "blocked_symlink", "blocked_unsafe_layout", "blocked_collision",
        "blocked_nonregular", "blocked_oversize", "blocked_io",
    )
    counts = {"total": 0, "eligible": 0, "copied": 0, "blocked": 0, "skipped": 0}
    counts.update({reason: 0 for reason in reasons})
    counts.update({"outside_python_scripts": 0, "outside_python_tools": 0,
                   "outside_python_other": 0})
    counts.update({key: 0 for key in OUTSIDE_AUDIT_KEYS})

    for name in untracked_paths(app):
        counts["total"] += 1
        reason = _skip_reason(name)
        if reason:
            counts["skipped"] += 1
            counts[reason] += 1
            if reason == "skipped_python_outside_roots":
                root = PurePosixPath(name).parts[0]
                role = ("scripts" if root == "scripts" else
                        "tools" if root in {"tools", "tooling", "utilities"} else "other")
                counts["outside_python_" + role] += 1
                for key, value in _audit_outside_python(app, name).items():
                    counts[key] += value
            continue

        counts["eligible"] += 1
        source, target = app / name, candidate / name
        source_parents = [source, *list(source.parents)[:-1]]
        target_parents = [target, *list(target.parents)[:-1]]
        reason = None
        try:
            if (any(p.is_symlink() for p in source_parents if p != app)
                    or any(p.is_symlink() for p in target_parents if p != candidate)):
                reason = "blocked_symlink"
            elif (not source.resolve().is_relative_to(app)
                    or not target.parent.resolve().is_relative_to(candidate)):
                reason = "blocked_unsafe_layout"
            elif target.exists() or target.is_symlink():
                reason = "blocked_collision"
            else:
                info = source.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    reason = "blocked_nonregular"
                elif info.st_size > MAX_SOURCE_BYTES:
                    reason = "blocked_oversize"
                else:
                    data = source.read_bytes()
                    if len(data) > MAX_SOURCE_BYTES:
                        reason = "blocked_oversize"
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("xb") as handle:
                            handle.write(data)
                        target.chmod(0o600)
                        counts["copied"] += 1
        except (OSError, ValueError):
            reason = "blocked_io"
        if reason:
            counts["blocked"] += 1
            counts[reason] += 1
    return counts


def main() -> int:
    if len(sys.argv) != 3:
        print("untracked_overlay=invalid_arguments")
        return 2
    try:
        counts = stage(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, subprocess.SubprocessError):
        print("untracked_overlay=blocked")
        return 3
    print("untracked_overlay=staged_private_candidate")
    for key, value in counts.items():
        print(f"untracked_overlay_{key}={value}")
    print("untracked_overlay_production_unchanged=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
