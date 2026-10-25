#!/usr/bin/env python3
"""Summarize conflicting Router methods without exposing local source or secrets."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def git_source(app: str, ref: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", app, "show", f"{ref}:{path}"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else None


def router_methods(source: str | None) -> dict[str, str] | None:
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Router":
            return {
                item.name: ast.dump(item, include_attributes=False)
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return {}


def changed_methods(base: dict[str, str], other: dict[str, str]) -> set[str]:
    return {
        name for name in base.keys() | other.keys()
        if base.get(name) != other.get(name)
    }


def main() -> int:
    if len(sys.argv) != 4:
        print("router_symbol_diagnostics=invalid_arguments")
        return 2
    app, base, upstream = sys.argv[1:]
    path = "router/router.py"
    try:
        base_methods = router_methods(git_source(app, base, path))
        upstream_methods = router_methods(git_source(app, upstream, path))
        local_methods = router_methods((Path(app) / path).read_text())
    except (OSError, UnicodeError):
        print("router_symbol_diagnostics=unavailable")
        return 0
    if any(value is None for value in (base_methods, upstream_methods, local_methods)):
        print("router_symbol_diagnostics=unavailable")
        return 0
    local_changed = changed_methods(base_methods, local_methods)
    upstream_changed = changed_methods(base_methods, upstream_methods)
    overlap = local_changed & upstream_changed
    local_only = local_changed - upstream_changed
    print("router_symbol_diagnostics=available")
    print(f"router_local_modified_method_count={len(local_changed)}")
    print(f"router_upstream_modified_method_count={len(upstream_changed)}")
    print(f"router_overlapping_modified_method_count={len(overlap)}")
    # Only Python identifiers, never method bodies, diffs, environment or keys.
    print("router_overlapping_methods=" + ",".join(sorted(overlap)[:40]))
    print("router_local_only_modified_methods=" + ",".join(sorted(local_only)[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
