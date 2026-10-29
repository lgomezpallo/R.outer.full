#!/usr/bin/env python3
"""Stage the frozen Router release on Oracle without touching the running checkout.

Only a private snapshot, a detached Git worktree and an unprivileged loopback
sidecar are created. This program NEVER switches router.service or edits router.db.
"""
from __future__ import annotations

import getpass
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
import uuid
from pathlib import Path

RELEASE_BRANCH = "feat/offline-cooldown-restart-consistency-20261028"


def candidate_pin() -> str:
    """Require an explicit immutable candidate SHA; never silently stage the old baseline."""
    sha = os.environ.get("ROUTER_RELEASE_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StageBlocked("candidate_sha_required")
    return sha
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


def effective_service_data_paths(raw: bytes, app: Path) -> dict[str, Path]:
    """Select only Router data locations; never log or retain credential values."""
    wanted = {b"ROUTER_DB_FILE": "db", b"ROUTER_CATALOG_FILE": "catalog"}
    selected: dict[str, Path] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if not sep or key not in wanted or not value:
            continue
        path = Path(os.fsdecode(value))
        if not path.is_absolute():
            path = app / path
        selected[wanted[key]] = path
    if set(selected) != {"db", "catalog"}:
        raise StageBlocked("effective_data_paths_unverified")
    return selected


def service_data_paths() -> dict[str, Path]:
    """Read the running service's effective paths, not SSH runner env files."""
    try:
        pid = int(command("systemctl", "show", "router.service", "-p", "MainPID", "--value"))
        if pid <= 0:
            raise StageBlocked("service_pid_unavailable")
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except (ValueError, OSError, subprocess.CalledProcessError):
        raise StageBlocked("effective_data_paths_unavailable") from None
    return effective_service_data_paths(raw, APP)


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


def backup_checkout(app: Path, snapshot: Path, base: str, active: dict[str, Path] | None = None) -> dict[str, int]:
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
    if active is not None:
        db = active["db"]
        if not sqlite_snapshot(db, snapshot / "effective-router.db"):
            raise StageBlocked("effective_database_missing")
        catalog = active["catalog"]
        if catalog.is_symlink() or not catalog.is_file():
            raise StageBlocked("effective_catalog_missing_or_unsafe")
        shutil.copyfile(catalog, snapshot / "effective-catalog.json")
        (snapshot / "effective-catalog.json").chmod(0o600)
        print("effective_data_snapshot=created", flush=True)
    for item in snapshot.iterdir():
        if item.is_file():
            item.chmod(0o600)
    return {"untracked": len(names)}


def isolated_sidecar(release: Path, python: Path, snapshot: Path, label: str) -> bool:
    """Run a disposable, OS-network-isolated service on the sidecar port.

    This never edits router.service. Each invocation uses a fresh private DB,
    no provider credentials and a transient systemd unit restricted to loopback.
    """
    if not port_free(SIDECAR_PORT):
        raise StageBlocked("sidecar_port_busy")
    # Exercise both code versions against a *copy of the effective live-data
    # snapshot*, never a new empty DB. Recreate the copy for baseline rollback
    # so candidate migrations cannot contaminate the restored baseline.
    source_db = snapshot / "effective-router.db"
    source_catalog = snapshot / "effective-catalog.json"
    sidecar_db = snapshot / f"sidecar-{label}.db"
    sidecar_catalog = snapshot / f"sidecar-{label}-catalog.json"
    for source, destination in ((source_db, sidecar_db),
                                (source_catalog, sidecar_catalog)):
        if source.is_symlink() or not source.is_file() or destination.exists() or destination.is_symlink():
            raise StageBlocked("sidecar_snapshot_unsafe")
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    with sqlite3.connect(sidecar_db) as restored:
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StageBlocked("sidecar_snapshot_integrity_failed")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")
           if key in os.environ}
    env.update({
        "PYTHONPATH": str(release),
        "ROUTER_OFFLINE_ONLY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ROUTER_DB_FILE": str(sidecar_db),
        "ROUTER_CATALOG_FILE": str(sidecar_catalog),
        "ROUTER_STARTUP_PROVIDER_TESTS": "0",
        "ROUTER_STARTUP_ROUTING_SMOKE": "0",
        "ROUTER_STARTUP_CAPABILITY_PROFILE": "0",
        "ROUTER_STARTUP_ORCHESTRATION_SMOKE": "0",
        "ROUTER_STARTUP_HETEROGENEOUS_SMOKE": "0",
    })
    unit = "router-stage-sidecar-" + uuid.uuid4().hex[:12]
    args = [
        "sudo", "-n", "systemd-run", "--quiet", "--collect",
        "--unit=" + unit,
        "--property=User=" + getpass.getuser(),
        "--property=WorkingDirectory=" + str(release),
        "--property=NoNewPrivileges=yes",
        "--property=IPAddressDeny=any",
        "--property=IPAddressAllow=localhost",
        "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=" + str(snapshot),
        "--property=PrivateTmp=yes",
    ]
    args.extend("--setenv=" + key + "=" + value for key, value in env.items())
    args += [str(python), "-m", "uvicorn", "router.api:app", "--host", "127.0.0.1",
             "--port", str(SIDECAR_PORT)]
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=15)
    try:
        for _ in range(30):
            if health(SIDECAR_PORT):
                return True
            if subprocess.run(["systemctl", "is-active", "--quiet", unit],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode != 0:
                return False
            time.sleep(1)
        return False
    finally:
        subprocess.run(["sudo", "-n", "systemctl", "stop", unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
        for _ in range(15):
            if port_free(SIDECAR_PORT):
                break
            time.sleep(0.2)
        else:
            raise StageBlocked("sidecar_not_stopped")


def stage() -> None:
    pin = candidate_pin()
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
    if fetched != pin:
        raise StageBlocked("frozen_release_sha_mismatch")
    base = command("git", "merge-base", original_head, pin, cwd=APP)
    counts = backup_checkout(APP, snapshot, base, active=service_data_paths())
    print("private_snapshot=created", flush=True)
    print(f"private_untracked_preserved={counts['untracked']}", flush=True)
    release = release_root / pin
    if release.exists():
        if release.is_symlink() or command("git", "rev-parse", "HEAD", cwd=release) != pin:
            raise StageBlocked("existing_release_not_pinned")
    else:
        command("git", "worktree", "add", "--detach", str(release), pin, cwd=APP)
    print("frozen_release_worktree=staged", flush=True)
    # The tests assert the normal billing-denial reason, so do NOT set
    # ROUTER_OFFLINE_ONLY for pytest: it changes the expected error codes.
    # Instead, run the entire suite in a transient OS-level network sandbox,
    # with no provider credentials and no access to non-loopback IP addresses.
    env = {
        "PYTHONPATH": str(release),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ROUTER_PYTHON": str(python),
        "ROUTER_DB_FILE": str(snapshot / "pytest.db"),
        "ROUTER_CATALOG_FILE": str(snapshot / "pytest-catalog.json"),
        "HOME": str(Path.home()),
    }
    unit = "router-stage-pytest-" + uuid.uuid4().hex[:12]
    sandbox = [
        "sudo", "-n", "systemd-run", "--quiet", "--pipe", "--wait", "--collect",
        "--unit=" + unit,
        "--property=User=" + getpass.getuser(),
        "--property=WorkingDirectory=" + str(release),
        "--property=NoNewPrivileges=yes",
        "--property=IPAddressDeny=any",
        "--property=IPAddressAllow=localhost",
        "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=" + str(snapshot),
        "--property=PrivateTmp=yes",
    ]
    sandbox.extend("--setenv=" + key + "=" + value for key, value in env.items())
    sandbox += [str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    with (snapshot / "pytest.log").open("wb") as log:
        result = subprocess.run(
            sandbox, cwd=release, stdout=log, stderr=subprocess.STDOUT,
            timeout=180,
        )
    print(f"staged_pytest={'passed' if result.returncode == 0 else 'failed'}", flush=True)
    if result.returncode:
        # Only expose public test identifiers; private assertion text stays on Oracle.
        pattern = re.compile(r"^(?:FAILED|ERROR) (tests/[A-Za-z0-9_./\[\]-]+(?:::[A-Za-z0-9_./\[\]-]+)*)")
        cases = []
        for line in (snapshot / "pytest.log").read_text(errors="replace").splitlines():
            match = pattern.match(line)
            if match:
                cases.append(match.group(1))
        print(f"staged_pytest_failure_count={len(cases)}", flush=True)
        for case in cases[:12]:
            print(f"staged_pytest_case={case}", flush=True)
        raise StageBlocked("staged_pytest_failed")
    # Rehearse baseline -> pinned candidate -> baseline on one disposable port.
    # This is a real process rollback rehearsal, NOT a production unit switch.
    for source, label in ((APP, "baseline-before"), (release, "candidate"),
                          (APP, "baseline-restored")):
        passed = isolated_sidecar(source, python, snapshot, label)
        print(f"sidecar_{label.replace('-', '_')}={'passed' if passed else 'failed'}",
              flush=True)
        if not passed:
            raise StageBlocked("sidecar_" + label.replace("-", "_") + "_failed")
    print("isolated_health=passed", flush=True)
    print("isolated_process_rollback_rehearsal=passed", flush=True)
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
