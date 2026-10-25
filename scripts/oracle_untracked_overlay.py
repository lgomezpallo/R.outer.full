#!/usr/bin/env python3
"""Privately stage a narrow, safe subset of Oracle-only source into a test worktree.

Never modifies the live checkout, never prints filenames or file contents,
never stages credentials/configuration, and never overwrites upstream files.
The result is only a diagnostic candidate, NOT a deployable release.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys

MAX_SOURCE_BYTES = 256_000
ALLOWED_ROOTS = frozenset({"router", "tests"})


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
        and not any(part in {"__pycache__", "venv", "node_modules", "secrets"} for part in path.parts)
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
    if is_safe_source_path(name):
        return None
    if path.suffix == ".py":
        return "skipped_python_outside_roots"
    if path.suffix in SOURCE_LIKE_SUFFIXES:
        return "skipped_other_source_like"
    return "skipped_other"


def stage(app: Path, candidate: Path) -> dict[str, int]:
    app, candidate = app.resolve(), candidate.resolve()
    if (not app.is_dir() or not candidate.is_dir()
            or candidate.is_relative_to(app) or app.is_relative_to(candidate)):
        raise ValueError("unsafe worktree layout")

    reasons = (
        "skipped_unsafe_path", "skipped_python_outside_roots",
        "skipped_other_source_like", "skipped_other",
        "blocked_symlink", "blocked_unsafe_layout", "blocked_collision",
        "blocked_nonregular", "blocked_oversize", "blocked_io",
    )
    counts = {"total": 0, "eligible": 0, "copied": 0, "blocked": 0, "skipped": 0}
    counts.update({reason: 0 for reason in reasons})

    for name in untracked_paths(app):
        counts["total"] += 1
        reason = _skip_reason(name)
        if reason:
            counts["skipped"] += 1
            counts[reason] += 1
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
