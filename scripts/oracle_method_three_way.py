#!/usr/bin/env python3
"""Three-way merge Router methods privately; never change the live checkout.

Only method bodies may differ from the common ancestor on the Oracle side.
If both sides change a method, require a clean line-level three-way merge.
The resulting candidate must pass tests before it can be considered deployable.
No private source, diffs or subprocess stderr are written to public logs.
"""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import tempfile


class MergeBlocked(Exception):
    """The local and upstream changes cannot be merged automatically."""


def method_ranges(source: str) -> dict[str, tuple[int, int]]:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Router":
            ranges = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                    ranges[item.name] = (start, item.end_lineno)
            return ranges
    raise MergeBlocked("missing Router class")


def method_text(source: str, bounds: tuple[int, int]) -> str:
    return "".join(source.splitlines(keepends=True)[bounds[0] - 1:bounds[1]])


def scaffold(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for name, (start, end) in sorted(method_ranges(source).items(), key=lambda x: x[1][0], reverse=True):
        lines[start - 1:end] = [f"    # method:{name}\n"]
    return "".join(lines)


def merge_lines(base: str, upstream: str, local: str) -> str:
    with tempfile.TemporaryDirectory(prefix="router-method-lines-") as folder:
        files = [Path(folder) / name for name in ("local", "base", "upstream")]
        for path, content in zip(files, (local, base, upstream)):
            path.write_text(content, encoding="utf-8")
        result = subprocess.run(
            ["git", "merge-file", "-p", *map(str, files)],
            text=True, capture_output=True, check=False,
        )
        if result.returncode != 0:
            raise MergeBlocked("overlapping edits")
        return result.stdout


def merge_sources(base: str, upstream: str, local: str) -> tuple[str, int]:
    # Reject Oracle-side changes to imports, class attributes, method order,
    # additional methods or any other code outside the original methods.
    # Such changes require separate human/code review; never discard them.
    base_ranges = method_ranges(base)
    local_ranges = method_ranges(local)
    upstream_ranges = method_ranges(upstream)
    if set(base_ranges) != set(local_ranges) or scaffold(base) != scaffold(local):
        raise MergeBlocked("local non-method changes")
    upstream_lines = upstream.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    overlaps = 0
    for name, local_bounds in local_ranges.items():
        old = method_text(base, base_ranges[name])
        current = method_text(local, local_bounds)
        if current == old:
            continue
        if name not in upstream_ranges:
            raise MergeBlocked("upstream method removed")
        upstream_method = method_text(upstream, upstream_ranges[name])
        if upstream_method == old:
            combined = current
        elif upstream_method == current:
            combined = upstream_method
        else:
            combined = merge_lines(old, upstream_method, current)
            overlaps += 1
        start, end = upstream_ranges[name]
        edits.append((start, end, combined))
    for start, end, combined in sorted(edits, reverse=True):
        upstream_lines[start - 1:end] = [combined]
    merged = "".join(upstream_lines)
    try:
        ast.parse(merged)
    except SyntaxError as exc:
        raise MergeBlocked("invalid merged syntax") from exc
    if scaffold(merged) != scaffold(upstream):
        raise MergeBlocked("upstream scaffold altered")
    return merged, overlaps


def git_show(app: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(app), "show", f"{ref}:router/router.py"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise MergeBlocked("source unavailable")
    return result.stdout


def main() -> int:
    if len(sys.argv) != 5:
        print("router_method_merge=invalid_arguments")
        return 2
    try:
        app = Path(sys.argv[1]).resolve()
        base, upstream = sys.argv[2:4]
        target = Path(sys.argv[4]).resolve()
        # Never write into the live Oracle tree or through a symlink.
        if target.is_relative_to(app) or Path(sys.argv[4]).is_symlink() or not target.is_file():
            raise MergeBlocked("unsafe target")
        merged, overlaps = merge_sources(
            git_show(app, base),
            git_show(app, upstream),
            (app / "router/router.py").read_text(encoding="utf-8"),
        )
        target.write_text(merged, encoding="utf-8")
        print("router_method_merge=clean")
        print(f"router_method_merge_overlap_count={overlaps}")
        print("router_method_merge_production_unchanged=yes")
        return 0
    except (MergeBlocked, OSError, UnicodeError, SyntaxError, ValueError, subprocess.SubprocessError):
        print("router_method_merge=blocked")
        print("router_method_merge_production_unchanged=yes")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
