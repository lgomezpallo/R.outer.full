#!/usr/bin/env python3
"""Privately test one colliding Oracle-only Python file in a disposable candidate.

Never changes the live Oracle checkout, never exports private code or filenames,
and never approves a merge or deployment. The caller supplies a *temporary*
worktree. Its original file is restored even if the test subprocess fails.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile

MAX_BYTES = 256_000
TEST_TIMEOUT = 180
SENSITIVE = re.compile(r"(?:secret|credential|token|private|api[_-]?key|password)", re.I)


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (not path.is_absolute() and len(path.parts) >= 2
            and path.parts[0] in {"router", "tests"} and path.suffix == ".py"
            and all(p not in {".", "..", "__pycache__", "venv", "node_modules"}
                    and not p.startswith(".") and not SENSITIVE.search(p)
                    for p in path.parts))


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=False)


def no_symlink_ancestors(root: Path, path: Path) -> bool:
    if not path.is_relative_to(root):
        return False
    current = path
    while current != root:
        if current.is_symlink():
            return False
        current = current.parent
    return True


def atomic_replace(path: Path, content: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=".router-collision-private-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def probe(app: Path, candidate: Path, python: Path) -> tuple[str, str]:
    app, candidate = app.resolve(), candidate.resolve()
    if (not app.is_dir() or not candidate.is_dir() or app == candidate
            or candidate.is_relative_to(app) or app.is_relative_to(candidate)
            or not python.is_file()):
        return "blocked_layout", "skipped"
    top = git(candidate, "rev-parse", "--show-toplevel")
    if top.returncode or Path(os.fsdecode(top.stdout.strip())).resolve() != candidate:
        return "blocked_layout", "skipped"
    untracked = git(app, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.returncode:
        return "blocked_inventory", "skipped"
    collisions = []
    for item in untracked.stdout.split(b"\0"):
        if not item:
            continue
        name = os.fsdecode(item)
        if safe_name(name) and (candidate / name).exists():
            collisions.append(name)
    if not collisions:
        return "none", "skipped"
    if len(collisions) != 1:
        return "multiple", "skipped"

    name = collisions[0]
    source, target = app / name, candidate / name
    if (not no_symlink_ancestors(app, source)
            or not no_symlink_ancestors(candidate, target)
            or not source.resolve().is_relative_to(app)
            or not target.resolve().is_relative_to(candidate)):
        return "blocked_path", "skipped"
    if git(candidate, "ls-files", "--error-unmatch", "--", name).returncode:
        return "blocked_untracked_target", "skipped"
    try:
        src_stat = source.stat(follow_symlinks=False)
        dst_stat = target.stat(follow_symlinks=False)
        if (not stat.S_ISREG(src_stat.st_mode) or not stat.S_ISREG(dst_stat.st_mode)
                or src_stat.st_size > MAX_BYTES or dst_stat.st_size > MAX_BYTES):
            return "blocked_file", "skipped"
        local_bytes, upstream_bytes = source.read_bytes(), target.read_bytes()
        if len(local_bytes) > MAX_BYTES or len(upstream_bytes) > MAX_BYTES:
            return "blocked_file", "skipped"
        if local_bytes == upstream_bytes:
            return "identical", "skipped"
        original_mode = stat.S_IMODE(dst_stat.st_mode)
        atomic_replace(target, local_bytes, original_mode)
        try:
            env = {**os.environ, "ROUTER_PYTHON": str(python)}
            try:
                outcome = subprocess.run(
                    [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                    cwd=candidate, env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=TEST_TIMEOUT, check=False,
                )
                test_status = "passed" if outcome.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                test_status = "timeout"
            except OSError:
                test_status = "interpreter_unavailable"
        finally:
            atomic_replace(target, upstream_bytes, original_mode)
        return "tested_private_variant", test_status
    except (OSError, ValueError):
        return "blocked_io", "skipped"


def main() -> int:
    if len(sys.argv) != 4:
        print("oracle_collision_status=invalid_arguments")
        return 2
    status, tests = probe(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    print("oracle_collision_status=" + status)
    print("oracle_collision_tests=" + tests)
    print("oracle_collision_production_unchanged=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
