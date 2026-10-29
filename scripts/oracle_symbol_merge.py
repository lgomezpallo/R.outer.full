#!/usr/bin/env python3
"""Fail-closed structural three-way merge of Python functions/methods.

Only run on disposable copies. Does not print private source or conflict hunks.
"""
from __future__ import annotations
import ast
import difflib
from collections import Counter
import textwrap
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

def merge_nonoverlapping(base: str, upstream: str, local: str) -> str:
    """Merge disjoint base edits, including compatible import insertions.

    No overlapping replacements are silently selected. This supplements git
    merge-file when nearby additions are reported as one conflicting hunk.
    """
    b = base.splitlines(keepends=True)
    u = upstream.splitlines(keepends=True)
    l = local.splitlines(keepends=True)

    def changes(other):
        return [(i, j, other[k:m]) for tag, i, j, k, m in
                difflib.SequenceMatcher(None, b, other, autojunk=False).get_opcodes()
                if tag != 'equal']

    uc, lc = changes(u), changes(l)
    edits = list(uc)
    for a, z, value in lc:
        duplicate = False
        for i, j, existing in uc:
            if a == i and z == j and value == existing:
                duplicate = True
                break
            collision = (a < j and i < z) or (a == z and i <= a < j) or (i == j and a <= i < z) or (a == z == i == j)
            if not collision:
                continue
            if a == z == i == j and _only_imports(value) and _only_imports(existing):
                combined = existing + [line for line in value if line not in existing]
                edits = [(x, y, combined if (x, y) == (i, j) else v) for x, y, v in edits]
                duplicate = True
                break
            raise Blocked('overlapping_base_edits')
        if not duplicate:
            edits.append((a, z, value))
    output = list(b)
    for a, z, value in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        output[a:z] = value
    return ''.join(output)


def _only_imports(lines: list[str]) -> bool:
    content = ''.join(lines)
    try:
        parsed = ast.parse(content)
    except SyntaxError:
        return False
    return bool(parsed.body) and all(isinstance(node, (ast.Import, ast.ImportFrom))
                                     for node in parsed.body)


def merge_lines(base: str, upstream: str, local: str) -> str:
    with tempfile.TemporaryDirectory(prefix='safe-symbol-') as folder:
        paths = [Path(folder) / x for x in ('local','base','upstream')]
        for path, content in zip(paths, (local,base,upstream)):
            path.write_text(content, encoding='utf-8')
        result = subprocess.run(['git','merge-file','-p',*map(str,paths)],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout
        if result.returncode >= 128:
            raise Blocked('merge_tool_failed')
        return merge_nonoverlapping(base, upstream, local)

def oracle_semantically_subsumed(base: str, upstream: str, local: str) -> bool:
    """Conservative proof that every changed Oracle statement exists upstream.

    This does NOT infer equivalence of different code. Any local-only statement,
    retained removed statement, or formatting-only edit blocks this fallback.
    """
    if local == base:
        return True
    if upstream == local:
        return True
    try:
        trees = [ast.parse(textwrap.dedent(x)) for x in (base, upstream, local)]
    except (SyntaxError, ValueError):
        return False

    def statements(tree):
        return Counter(ast.dump(node, include_attributes=False) for node in ast.walk(tree)
                       if isinstance(node, ast.stmt) and not isinstance(
                           node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    old, current, new = map(statements, trees)
    additions = new - old
    deletions = old - new
    if not additions and not deletions:
        return False  # Oracle changed comments/formatting; do not discard.
    return all(current[key] >= count for key, count in additions.items()) and all(
        current[key] <= old[key] - count for key, count in deletions.items()
    )

def merge_statements(base: str, upstream: str, local: str) -> str:
    """Last resort: combine distinct whole Python statements within one method.

    Never merges two edits to the same statement or silently drops comments.
    """
    def parse(source: str):
        tree = ast.parse(textwrap.dedent(source))
        if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise Blocked('not_single_function')
        function = tree.body[0]
        lines = source.splitlines(keepends=True)
        if not function.body:
            raise Blocked('empty_body')
        starts = [node.lineno - 1 for node in function.body]
        prefix = ''.join(lines[:starts[0]])
        blocks = [''.join(lines[a:b]) for a, b in zip(starts, starts[1:] + [len(lines)])]
        return prefix, blocks

    bp, bb = parse(base)
    up, ub = parse(upstream)
    lp, lb = parse(local)
    if lp == bp:
        prefix = up
    elif up == bp or up == lp:
        prefix = lp
    else:
        raise Blocked('overlapping_function_header')
    def edits(other):
        return [(i, j, other[k:m]) for tag, i, j, k, m in
                difflib.SequenceMatcher(None, bb, other, autojunk=False).get_opcodes()
                if tag != 'equal']
    uc, lc = edits(ub), edits(lb)
    result_edits = list(uc)
    for a, z, value in lc:
        duplicate = False
        for i, j, existing in uc:
            if (a, z, value) == (i, j, existing):
                duplicate = True
                break
            if (a < j and i < z) or (a == z == i == j) or (
                a == z and i <= a < j) or (i == j and a <= i < z):
                raise Blocked('overlapping_statements')
        if not duplicate:
            result_edits.append((a, z, value))
    merged = list(bb)
    for a, z, value in sorted(result_edits, key=lambda item: (item[0], item[1]), reverse=True):
        merged[a:z] = value
    output = prefix + ''.join(merged)
    ast.parse(textwrap.dedent(output))
    return output


def strict_retryable_line_subsumed(base: str, upstream: str, local: str) -> bool:
    """Preserve Oracle's sole retryable-status edit if upstream has it verbatim."""
    old, new = base.splitlines(keepends=True), local.splitlines(keepends=True)
    edits = [(tag, i, j, k, m) for tag, i, j, k, m in
             difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
             if tag != 'equal']
    if len(edits) != 1:
        return False
    tag, i, j, k, m = edits[0]
    if tag != 'replace' or j - i != 1 or m - k != 1:
        return False
    def eligible(line):
        return re.fullmatch(r'\s*retryable\s*=\s*status\s+in\s+\{[0-9, ]+\}\s*', line.strip()) is not None
    return (eligible(old[i]) and eligible(new[k]) and
            new[k].strip() in {line.strip() for line in upstream.splitlines()})


def strict_trace_extension_subsumed(upstream: str, local: str) -> bool:
    """Accept only the known provider-fallback trace extended by model fallback.

    Require byte-for-byte agreement of the core SQL/metric code. The only
    Oracle-specific tail permitted is the provider-only fallback computation.
    """
    marker = '        for trace in traces.values():\n'
    if upstream.count(marker) != 1 or local.count(marker) != 1:
        return False
    up_prefix, up_tail = upstream.split(marker, 1)
    lo_prefix, lo_tail = local.split(marker, 1)
    extra = '                "provider_fallback": False, "model_fallback": False,\n'
    if up_prefix.count(extra) != 1 or up_prefix.replace(extra, '') != lo_prefix:
        return False
    expected_oracle = (
        '            trace["fallback"] = len({a["provider"] for a in trace["attempts"]}) > 1\n'
        '        return [traces[row["request_id"]] for row in ids]\n'
    )
    if lo_tail != expected_oracle:
        return False
    # Candidate must explicitly preserve provider-only fallback as a separate
    # field while reporting broader fallback for model switching.
    required = (
        'trace["provider_fallback"] = len(providers) > 1',
        'trace["model_fallback"] = any(len(models) > 1 for models in models_by_provider.values())',
        'trace["fallback"] = trace["provider_fallback"] or trace["model_fallback"]',
        'return [traces[row["request_id"]] for row in ids]',
    )
    return all(up_tail.count(fragment) == 1 for fragment in required)


def scaffold_semantically_identical(a: str, b: str) -> bool:
    """Require identical non-method Python AST and preserved non-marker comments."""
    def normalize(source: str):
        marker = re.compile(r'^([ \t]*)# @@symbol:[A-Za-z_][\w.]*[ \t]*$', re.M)
        return marker.sub(lambda match: match.group(1) + 'pass', source)
    try:
        if ast.dump(ast.parse(normalize(a)), include_attributes=False) != ast.dump(
                ast.parse(normalize(b)), include_attributes=False):
            return False
    except SyntaxError:
        return False
    def comments(source):
        return [line.strip() for line in source.splitlines()
                if line.lstrip().startswith('#') and not line.lstrip().startswith('# @@symbol:')]
    return all(comment in comments(a) for comment in comments(b))


def combine(base: str, upstream: str, local: str) -> tuple[str,int]:
    b,u,l = symbols(base),symbols(upstream),symbols(local)
    try:
        merged_scaffold = merge_lines(scaffold(base),scaffold(upstream),scaffold(local))
    except Blocked as exc:
        raise Blocked('scaffold_' + str(exc)) from exc
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
            try:
                selected = merge_lines(old or '',current,new)
            except Blocked as exc:
                if (oracle_semantically_subsumed(old or '', current, new) or
                    (name == 'Router._classify_failure' and old is not None and
                     strict_retryable_line_subsumed(old, current, new)) or
                    (name == 'RouterStore.recent_request_traces' and old is None and
                     strict_trace_extension_subsumed(current, new))):
                    selected = current
                else:
                    try:
                        selected = merge_statements(old or '', current, new)
                    except (Blocked, SyntaxError, ValueError):
                        raise Blocked('symbol_' + str(exc) + '_at_' + name) from exc
            overlap += 1
        if selected is None:
            raise Blocked('missing_symbol')
        return selected.rstrip('\n') + '\n'
    combined = re.sub(r'^[ \t]*# @@symbol:([A-Za-z_][\w.]*)[ \t]*$',replacement,
                      merged_scaffold,flags=re.MULTILINE)
    combined = combined.rstrip('\n') + '\n'
    try:
        ast.parse(combined)
    except SyntaxError as exc:
        raise Blocked('invalid_python') from exc
    if scaffold(combined) != merged_scaffold:
        def meaningful_lines(value):
            return [line.rstrip() for line in value.splitlines() if line.strip()]
        if (meaningful_lines(scaffold(combined)) == meaningful_lines(merged_scaffold)
                or scaffold_semantically_identical(scaffold(combined), merged_scaffold)):
            return combined, overlap
        if oracle_semantically_subsumed(base, upstream, local):
            return upstream, overlap
        raise Blocked('scaffold_mismatch')
    return combined,overlap

def git_show(root: Path, rev: str, path: str) -> str:
    p = subprocess.run(['git','-C',str(root),'show',rev+':'+path],
                       capture_output=True,text=True,check=False)
    if p.returncode:
        raise Blocked('missing_base_source')
    return p.stdout

def safe_overlap_diagnostic(base: str, upstream: str, local: str, relative: str) -> None:
    """Only predefined non-secret HTTP status sets and known trace flags."""
    if relative not in {'router/router.py', 'router/storage.py'}:
        return
    target = ('Router._classify_failure' if relative == 'router/router.py'
              else 'RouterStore.recent_request_traces')
    for label, source in (('base', base), ('candidate', upstream), ('oracle', local)):
        try:
            source_symbol = symbols(source).get(target)
            if source_symbol is None:
                continue
            tree = ast.parse(textwrap.dedent(source_symbol))
            if relative == 'router/router.py':
                sets = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Set) and all(
                        isinstance(e, ast.Constant) and type(e.value) is int
                        and 100 <= e.value <= 599 for e in node.elts
                    ):
                        sets.append(tuple(sorted(e.value for e in node.elts)))
                print('diagnostic_' + label + '_http_sets=' +
                      ';'.join(','.join(map(str, values)) for values in sets[:12]))
            else:
                allowed = {'fallback', 'provider_fallback', 'model_fallback',
                           'attempts', 'request_id', 'created_at'}
                keys = {node.value for node in ast.walk(tree)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value in allowed}
                print('diagnostic_' + label + '_trace_keys=' + ','.join(sorted(keys)))
        except (SyntaxError, ValueError):
            print('diagnostic_' + label + '=unavailable')


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
        if isinstance(exc, Blocked) and relative in {'router/router.py', 'router/storage.py'}:
            try:
                safe_overlap_diagnostic(git_show(root,base,relative),
                                        git_show(root,upstream,relative),
                                        (root/relative).read_text(encoding='utf-8'), relative)
            except (OSError,UnicodeError,ValueError,SyntaxError):
                print('diagnostic=unavailable')
        print('symbol_merge=blocked_'+(str(exc) if isinstance(exc,Blocked) else 'invalid_source'))
        return 3

if __name__ == '__main__':
    raise SystemExit(main())
