#!/usr/bin/env python3
"""Read-only Oracle snapshot and disposable GitHub reconciliation.

The stream mode runs over SSH and writes tar to stdout. The reconcile mode
runs on a disposable Actions runner and never extracts untracked files or
prints private source. Encrypt the tar before storing it.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tarfile
import tempfile

APP = Path('/home/ubuntu/Proyecto/Herramientas/Router')
MAX_TOTAL = 100 * 1024 * 1024
MAX_FILE = 40 * 1024 * 1024


def git(repo: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(['git', '-C', str(repo), *args], input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=False)


def required(result: subprocess.CompletedProcess[bytes]) -> bytes:
    if result.returncode:
        raise RuntimeError('git operation failed')
    return result.stdout


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o600
    entry.mtime = 0
    tar.addfile(entry, io.BytesIO(data))


def stream() -> None:
    root = APP.resolve(strict=True)
    if Path(os.fsdecode(required(git(root, 'rev-parse', '--show-toplevel')).strip())).resolve() != root:
        raise RuntimeError('unexpected repository')
    head = required(git(root, 'rev-parse', 'HEAD')).decode('ascii').strip()
    patch = required(git(root, 'diff', '--binary', 'HEAD', '--'))
    tracked = [p for p in required(git(root, 'diff', '--name-only', '-z', 'HEAD', '--')).split(b'\0') if p]
    untracked = [os.fsdecode(p) for p in required(git(root, 'ls-files', '--others', '--exclude-standard', '-z')).split(b'\0') if p]
    entries: list[tuple[str, Path, os.stat_result, str | None]] = []
    total = len(patch)
    for relative in untracked:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in ('.', '..') for part in pure.parts):
            raise RuntimeError('unsafe path')
        path = root.joinpath(*pure.parts)
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > MAX_FILE:
                raise RuntimeError('oversized file')
            total += metadata.st_size
            link = None
        elif stat.S_ISLNK(metadata.st_mode):
            link = os.readlink(path)
            total += len(link.encode('utf-8', 'surrogateescape'))
        else:
            raise RuntimeError('unsupported file type')
        if total > MAX_TOTAL:
            raise RuntimeError('snapshot too large')
        entries.append((relative, path, metadata, link))
    manifest = {
        'format': 1, 'oracle_head': head, 'tracked_count': len(tracked),
        'untracked_count': len(entries), 'patch_sha256': hashlib.sha256(patch).hexdigest(),
    }
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as tar:
        add_bytes(tar, 'manifest.json', json.dumps(manifest, sort_keys=True).encode())
        add_bytes(tar, 'tracked.patch', patch)
        for relative, path, metadata, link in entries:
            entry = tarfile.TarInfo('untracked/' + relative)
            entry.mode = metadata.st_mode & 0o777
            entry.mtime = 0
            if link is not None:
                entry.type = tarfile.SYMTYPE
                entry.linkname = link
                tar.addfile(entry)
            else:
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as f:
                    current = os.fstat(f.fileno())
                    if not stat.S_ISREG(current.st_mode) or current.st_size != metadata.st_size:
                        raise RuntimeError('source changed during snapshot')
                    entry.size = current.st_size
                    tar.addfile(entry, f)


def export_private_reconciliation(candidate: Path) -> None:
    """Export only tracked, resolved changes to a runner-private patch."""
    output = os.environ.get('ROUTER_RECONCILE_PATCH_PATH')
    if not output:
        return
    runner_temp = os.environ.get('RUNNER_TEMP')
    if not runner_temp:
        raise RuntimeError('runner temp missing')
    destination = Path(output).resolve()
    if not destination.is_relative_to(Path(runner_temp).resolve()):
        raise RuntimeError('unsafe private patch destination')
    if git(candidate, 'diff', '--check', 'HEAD', '--').returncode:
        print('reconciled_whitespace_warning=yes')
    patch = required(git(candidate, 'diff', '--binary', 'HEAD', '--'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(patch)
    changed = [p for p in required(git(candidate, 'diff', '--name-only', '-z', 'HEAD', '--')).split(b'\0') if p]
    print('reconciled_tracked_file_count=' + str(len(changed)))
    print('reconciled_private_patch=exported_no_source_logged')


def reconcile(archive: Path, candidate_sha: str) -> int:
    with tarfile.open(archive, mode='r:') as tar:
        names = tar.getnames()
        if names.count('manifest.json') != 1 or names.count('tracked.patch') != 1:
            raise RuntimeError('invalid snapshot structure')
        manifest_file = tar.extractfile('manifest.json')
        patch_file = tar.extractfile('tracked.patch')
        if manifest_file is None or patch_file is None:
            raise RuntimeError('invalid snapshot members')
        manifest = json.load(manifest_file)
        patch = patch_file.read(MAX_TOTAL + 1)
        if len(patch) > MAX_TOTAL or hashlib.sha256(patch).hexdigest() != manifest['patch_sha256']:
            raise RuntimeError('snapshot checksum mismatch')
        if manifest['format'] != 1 or len([n for n in names if n.startswith('untracked/')]) != manifest['untracked_count']:
            raise RuntimeError('snapshot inventory mismatch')
    root = Path.cwd()
    oracle_head = manifest['oracle_head']
    if not isinstance(oracle_head, str) or len(oracle_head) != 40 or any(c not in '0123456789abcdef' for c in oracle_head):
        raise RuntimeError('invalid oracle head')
    if not isinstance(candidate_sha, str) or len(candidate_sha) != 40 or any(c not in '0123456789abcdef' for c in candidate_sha):
        raise RuntimeError('invalid candidate head')
    required(git(root, 'cat-file', '-e', oracle_head + '^{commit}'))
    required(git(root, 'cat-file', '-e', candidate_sha + '^{commit}'))
    print('oracle_head=' + oracle_head[:12])
    print('candidate_head=' + candidate_sha[:12])
    print('tracked_snapshot_count=' + str(manifest['tracked_count']))
    print('untracked_encrypted_snapshot_count=' + str(manifest['untracked_count']))
    with tempfile.TemporaryDirectory(prefix='router-oracle-reconcile-') as temp:
        temp_root = Path(temp)
        oracle = temp_root / 'oracle'
        candidate = temp_root / 'candidate'
        required(git(root, 'worktree', 'add', '--detach', str(oracle), oracle_head))
        try:
            if patch:
                required(git(oracle, 'apply', '--binary', '--index', '-', data=patch))
            base = required(git(root, 'merge-base', oracle_head, candidate_sha)).decode().strip()
            local_delta = required(git(oracle, 'diff', '--binary', base, '--'))
            required(git(root, 'worktree', 'add', '--detach', str(candidate), candidate_sha))
            try:
                result = git(candidate, 'apply', '--3way', '--index', '-', data=local_delta)
                if result.returncode == 0:
                    export_private_reconciliation(candidate)
                    print('tracked_reconcile=applied_to_isolated_candidate')
                    return 0
                unmerged = required(git(candidate, 'ls-files', '-u', '-z'))
                paths = set()
                for record in unmerged.split(b'\0'):
                    if record and b'\t' in record:
                        paths.add(os.fsdecode(record.split(b'\t', 1)[1]))
                print('tracked_conflict_count=' + str(len(paths)))
                # Paths are repo-relative names only: never print the patch,
                # untracked file inventory, conflict hunks, or private source.
                print('tracked_conflict_files=' + ','.join(sorted(paths)))
                method_merged = False
                if 'router/router.py' in paths:
                    merger = root / 'scripts' / 'oracle_method_three_way.py'
                    result = subprocess.run([sys.executable, str(merger), str(oracle), base,
                                             candidate_sha, str(candidate / 'router/router.py')],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, check=False)
                    if result.returncode == 0:
                        required(git(candidate, 'add', '--', 'router/router.py'))
                        method_merged = True
                        print('router_method_merge=applied')
                    else:
                        print('router_method_merge=blocked')
                # Try a conservative symbol-aware merge for remaining Python
                # conflicts. Never print the Oracle source or commit it publicly.
                for relative in sorted(paths):
                    if relative == 'router/router.py' and method_merged:
                        continue
                    if not relative.startswith('router/') or not relative.endswith('.py'):
                        continue
                    helper = root / 'scripts' / 'oracle_symbol_merge.py'
                    attempt = subprocess.run(
                        [sys.executable, str(helper), str(oracle), base, candidate_sha,
                         relative, str(candidate / relative)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, check=False,
                    )
                    print('symbol_merge_file=' + relative)
                    print(attempt.stdout.strip() if attempt.stdout.strip() else 'symbol_merge=blocked')
                    if attempt.returncode == 0:
                        required(git(candidate, 'add', '--', relative))
                unmerged = required(git(candidate, 'ls-files', '-u', '-z'))
                remaining = set()
                for record in unmerged.split(b'\0'):
                    if record and b'\t' in record:
                        remaining.add(os.fsdecode(record.split(b'\t', 1)[1]))
                print('tracked_remaining_conflict_count=' + str(len(remaining)))
                print('tracked_remaining_conflict_files=' + ','.join(sorted(remaining)))
                if not remaining:
                    export_private_reconciliation(candidate)
                    print('tracked_reconcile=applied_to_isolated_candidate')
                    return 0
                print('tracked_reconcile=blocked_private_overlap')
                return 3
            finally:
                git(root, 'worktree', 'remove', '--force', str(candidate))
        finally:
            git(root, 'worktree', 'remove', '--force', str(oracle))


if __name__ == '__main__':
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'stream':
            stream()
        elif len(sys.argv) == 4 and sys.argv[1] == 'reconcile':
            sys.exit(reconcile(Path(sys.argv[2]), sys.argv[3]))
        else:
            raise RuntimeError('invalid invocation')
    except Exception:
        print('oracle_snapshot_or_reconcile=blocked', file=sys.stderr)
        sys.exit(2)
