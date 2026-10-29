#!/usr/bin/env python3
"""Fail-closed structural three-way merge of Python functions/methods.

Only run on disposable copies. Does not print private source or conflict hunks.
"""
from __future__ import annotations
import ast
from pathlib import Path
import re
import subprocess
import sys
import tempfile

class Blocked(Exception):
    pass

def symbols(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes = [(node.name, node)]
        elif isinstance(node, ast.ClassDef):
            nodes = [(node.name + '.' + m.name, m) for m in node.body
                     if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            nodes = []
        for name, item in nodes:
            start = min([item.lineno] + [d.lineno for d in item.decorator_list])
            if name in out:
                raise Blocked('duplicate_symbol')
            out[name] = ''.join(lines[start-1:item.end_lineno])
    return out

def scaffold(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    edits = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes = [(node.name, node)]
        elif isinstance(node, ast.ClassDef):
            nodes = [(node.name + '.' + m.name, m) for m in node.body
                     if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        else:
            nodes = []
        for name, item in nodes:
            start = min([item.lineno] + [d.lineno for d in item.decorator_list])
            indent = re.match(r'[ \t]*', lines[start-1]).group(0)
            edits.append((start, item.end_lineno, indent + '# @@symbol:' + name + '\n'))
    for start, end, marker in sorted(edits, reverse=True):
        lines[start-1:end] = [marker]
    return ''.join(lines)

def merge_lines(base: str, upstream: str, local: str) -> str:
    with tempfile.TemporaryDirectory(prefix='safe-symbol-') as folder:
        paths = [Path(folder) / x for x in ('local','base','upstream')]
        for path, content in zip(paths, (local,base,upstream)):
            path.write_text(content, encoding='utf-8')
        result = subprocess.run(['git','merge-file','-p',*map(str,paths)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise Blocked('overlap')
        return result.stdout

def combine(base: str, upstream: str, local: str) -> tuple[str,int]:
    b,u,l = symbols(base),symbols(upstream),symbols(local)
    merged_scaffold = merge_lines(scaffold(base),scaffold(upstream),scaffold(local))
    overlap = 0
    def replacement(match):
        nonlocal overlap
        name = match.group(1)
        old, current, new = b.get(name),u.get(name),l.get(name)
        if new == old:
            selected = current
        elif current == old:
            selected = new
        elif current == new:
            selected = current
        elif current is None or new is None:
            raise Blocked('changed_deleted_symbol')
        else:
            selected = merge_lines(old or '',current,new)
            overlap += 1
        if selected is None:
            raise Blocked('missing_symbol')
        return selected.rstrip('\n') + '\n'
    combined = re.sub(r'^[ \t]*# @@symbol:([A-Za-z_][\w.]*)[ \t]*$',replacement,
                      merged_scaffold,flags=re.MULTILINE)
    try:
        ast.parse(combined)
    except SyntaxError as exc:
        raise Blocked('invalid_python') from exc
    if scaffold(combined) != merged_scaffold:
        raise Blocked('scaffold_mismatch')
    return combined,overlap

def git_show(root: Path, rev: str, path: str) -> str:
    p = subprocess.run(['git','-C',str(root),'show',rev+':'+path],
                       capture_output=True,text=True,check=False)
    if p.returncode:
        raise Blocked('missing_base_source')
    return p.stdout

def main() -> int:
    if len(sys.argv) != 6:
        print('symbol_merge=invalid_arguments')
        return 2
    root = Path(sys.argv[1]).resolve()
    base, upstream, relative = sys.argv[2:5]
    target = Path(sys.argv[5]).resolve()
    if not relative.startswith('router/') or not relative.endswith('.py') or target.is_relative_to(root) or Path(sys.argv[5]).is_symlink():
        print('symbol_merge=unsafe_target')
        return 3
    try:
        merged, overlaps = combine(git_show(root,base,relative),
                                   git_show(root,upstream,relative),
                                   (root/relative).read_text(encoding='utf-8'))
        target.write_text(merged,encoding='utf-8')
        print('symbol_merge=clean')
        print('symbol_merge_overlap_count='+str(overlaps))
        return 0
    except (Blocked,OSError,UnicodeError,ValueError,SyntaxError) as exc:
        print('symbol_merge=blocked_'+(str(exc) if isinstance(exc,Blocked) else 'invalid_source'))
        return 3

if __name__ == '__main__':
    raise SystemExit(main())
