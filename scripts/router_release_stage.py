#!/usr/bin/env python3
"""Stage the frozen Router release on Oracle without touching the running checkout.

Only a private snapshot, a detached Git worktree and an unprivileged loopback
sidecar are created. This program NEVER switches router.service or edits router.db.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

PINNED_SHA = "339898a6081061b41ad2557b600e06efbc1f00ef"
RELEASE_BRANCH = "release/router-baseline-20261025"
APP = Path("/home/ubuntu/Proyecto/Herramientas/Router")
DATA = Path("/home/ubuntu/Proyecto/Datos/Router")
SIDECAR_PORT = 18010


class StageBlocked(RuntimeError):
    pass


def command(*args: str, cwd: Path | None = None, output: Path | None = None) -> str:
    if output is None:
        return subprocess.check_output(args, cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()
    with output.open("wb") as stream:
        subprocess.run(args, cwd=cwd, stdout=stream, stderr=subprocess.DEVNULL, check=True)
    return ""


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise StageBlocked("unsafe_snapshot_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def sqlite_snapshot(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    if source.is_symlink() or not source.is_file():
        raise StageBlocked("unsafe_database_path")
    # SQLite's online backup API, not cp: handles a live WAL-mode database.
    uri = source.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=20) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    destination.chmod(0o600)
    return True


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=4
        ) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError, KeyError):
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def backup_checkout(app: Path, snapshot: Path, base: str) -> dict[str, int]:
    command("git", "rev-parse", "HEAD", cwd=app, output=snapshot / "oracle-head.txt")
    command("git", "status", "--porcelain=v1", "-z", cwd=app, output=snapshot / "status.bin")
    command("git", "diff", "--binary", "HEAD", "--", cwd=app, output=snapshot / "uncommitted.patch")
    command("git", "diff", "--binary", base, "--", cwd=app, output=snapshot / "all-local-tracked.patch")
    command("git", "bundle", "create", str(snapshot / "oracle-head.bundle"), "HEAD", cwd=app)
    names = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=app
    ).split(b"\0")
    names = [name for name in names if name]
    # GNU tar receives NUL-delimited paths through stdin; never dereference symlinks.
    with (snapshot / "untracked.tar").open("wb") as target:
        subprocess.run(
            ["tar", "--null", "--verbatim-files-from", "-C", str(app), "-T", "-", "-cf", "-"],
            input=b"\0".join(names) + (b"\0" if names else b""),
            stdout=target, stderr=subprocess.DEVNULL, check=True,
        )
    for source, label in (
        (app / "router.db", "checkout"),
        (DATA / "router.db", "data"),
    ):
        sqlite_snapshot(source, snapshot / f"{label}-router.db")
    for source, label in (
        (app / "router-catalog.json", "checkout-catalog.json"),
        (DATA / "router-catalog.json", "data-catalog.json"),
    ):
        if source.is_file() and not source.is_symlink():
            shutil.copy2(source, snapshot / label)
    for item in snapshot.iterdir():
        if item.is_file():
            item.chmod(0o600)
    return {"untracked": len(names)}


def isolated_sidecar(release: Path, python: Path, snapshot: Path) -> bool:
    if not port_free(SIDECAR_PORT):
        raise StageBlocked("sidecar_port_busy")
    env = dict(os.environ)
    # Do not use live databases, provider credentials or a production catalog.
    for key in tuple(env):
        if key.endswith("_API_KEY") or key.endswith("_TOKEN") or key in (
            "ROUTER_MASTER_KEY", "ROUTER_SERVICE_TOKEN", "ROUTER_IACHAT_TOKEN"
        ):
            env.pop(key, None)
    env.update({
        "PYTHONPATH": str(release),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ROUTER_DB_FILE": str(snapshot / "sidecar.db"),
        "ROUTER_CATALOG_FILE": str(snapshot / "sidecar-catalog.json"),
        "ROUTER_STARTUP_PROVIDER_TESTS": "0",
        "ROUTER_STARTUP_ROUTING_SMOKE": "0",
        "ROUTER_STARTUP_CAPABILITY_PROFILE": "0",
        "ROUTER_STARTUP_ORCHESTRATION_SMOKE": "0",
        "ROUTER_STARTUP_HETEROGENEOUS_SMOKE": "0",
    })
    with (snapshot / "sidecar.log").open("wb") as log:
        process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "router.api:app", "--host", "127.0.0.1",
             "--port", str(SIDECAR_PORT)],
            cwd=release, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            for _ in range(30):
                if process.poll() is not None:
                    return False
                if health(SIDECAR_PORT):
                    return True
                time.sleep(1)
            return False
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def stage() -> None:
    os.umask(0o077)
    if not APP.is_dir() or APP.is_symlink():
        raise StageBlocked("missing_or_unsafe_checkout")
    if command("git", "rev-parse", "--show-toplevel", cwd=APP) != str(APP):
        raise StageBlocked("unexpected_git_root")
    if command("systemctl", "is-active", "router.service") != "active" or not health(8010):
        raise StageBlocked("production_unhealthy")
    python = APP / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise StageBlocked("production_python_missing")
    backup_root = Path.home() / "router-private-backups"
    release_root = Path.home() / "router-releases"
    private_directory(backup_root)
    private_directory(release_root)
    snapshot = Path(tempfile.mkdtemp(prefix="router-stage-", dir=backup_root))
    snapshot.chmod(0o700)
    # Record the dirty state BEFORE fetching anything; preserve every untracked path.
    original_head = command("git", "rev-parse", "HEAD", cwd=APP)
    command("git", "fetch", "--quiet", "origin", f"refs/heads/{RELEASE_BRANCH}", cwd=APP)
    fetched = command("git", "rev-parse", "FETCH_HEAD", cwd=APP)
    if fetched != PINNED_SHA:
        raise StageBlocked("frozen_release_sha_mismatch")
    base = command("git", "merge-base", original_head, PINNED_SHA, cwd=APP)
    counts = backup_checkout(APP, snapshot, base)
    print("private_snapshot=created", flush=True)
    print(f"private_untracked_preserved={counts['untracked']}", flush=True)
    release = release_root / PINNED_SHA
    if release.exists():
        if release.is_symlink() or command("git", "rev-parse", "HEAD", cwd=release) != PINNED_SHA:
            raise StageBlocked("existing_release_not_pinned")
    else:
        command("git", "worktree", "add", "--detach", str(release), PINNED_SHA, cwd=APP)
    print("frozen_release_worktree=staged", flush=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(release),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ROUTER_DB_FILE": str(snapshot / "pytest.db"),
        "ROUTER_CATALOG_FILE": str(snapshot / "pytest-catalog.json"),
    })
    with (snapshot / "pytest.log").open("wb") as log:
        result = subprocess.run(
            [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=release, env=env, stdout=log, stderr=subprocess.STDOUT,
            timeout=180,
        )
    print(f"staged_pytest={'passed' if result.returncode == 0 else 'failed'}", flush=True)
    if result.returncode:
        raise StageBlocked("staged_pytest_failed")
    passed = isolated_sidecar(release, python, snapshot)
    print(f"isolated_health={'passed' if passed else 'failed'}", flush=True)
    if not passed:
        raise StageBlocked("isolated_health_failed")
    if command("git", "rev-parse", "HEAD", cwd=APP) != original_head:
        raise StageBlocked("production_head_changed")
    if command("systemctl", "is-active", "router.service") != "active" or not health(8010):
        raise StageBlocked("production_health_changed")
    print("production_service_unchanged=yes", flush=True)
    print("live_chat_and_fallback=not_yet_tested", flush=True)
    print("release_status=STAGED_NOT_DEPLOYED", flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] != "stage":
            raise StageBlocked("usage_stage_only")
        stage()
    except (StageBlocked, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, sqlite3.Error) as error:
        # Never print private paths, git output, tracebacks, credentials or source.
        code = str(error) if isinstance(error, StageBlocked) else type(error).__name__
        if not re.fullmatch(r"[a-z0-9_]+", code):
            code = "stage_operation_failed"
        print(f"release_stage_blocked={code}", flush=True)
        sys.exit(2)
