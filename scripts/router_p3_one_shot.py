#!/usr/bin/env python3
"""P3 one-shot systemd adapter, REVIEW ONLY.

No CLI, workflow or sudo entrypoint is provided. Importing this module does
nothing. A separately approved, root-owned immutable operator wrapper must
bind the fixed paths/SHA and call run_once. Never run from a writable checkout
under sudo. SIGKILL, power loss and a lost host require independent recovery.
"""
from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import router_execstart_reconcile as p3

UNIT = "router.service"
DROPINS = Path("/etc/systemd/system/router.service.d")
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8010


class OperationalBlock(Exception):
    """Only fixed labels may be surfaced to logs."""


class Interrupted(OperationalBlock):
    pass


@dataclass(frozen=True)
class Observed:
    unit_cat: bytes
    effective_exec: bytes
    paths: tuple[str, ...]
    dropins: dict[str, bytes]
    state: str
    health_ok: bool

    @property
    def offline(self) -> bool:
        return "ROUTER_OFFLINE_ONLY=1" in parse_effective(self.effective_exec)


def parse_effective(raw: bytes) -> list[str]:
    try:
        decoded = raw.decode("utf-8")
        match = re.search(r"argv\[\]=(.*?)\s+;\s+ignore_errors=", decoded)
        if not match:
            raise ValueError()
        return shlex.split(match.group(1))
    except (UnicodeError, ValueError):
        raise OperationalBlock("effective_exec_unparseable") from None


def _run(*args: str, timeout: int = 10) -> bytes:
    try:
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        raise OperationalBlock("system_command_failed") from None


def _regular_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OperationalBlock("dropin_not_regular")
            data = stream.read(65537)
        if len(data) > 65536:
            raise OperationalBlock("dropin_too_large")
        return data
    except OSError:
        raise OperationalBlock("dropin_unreadable") from None


class LiveSystemd:
    """Only fixed systemctl commands and fixed loopback health are permitted."""

    def __init__(self, directory: Path = DROPINS):
        if directory != DROPINS:
            raise OperationalBlock("live_dropin_directory_unexpected")
        self.directory = directory

    def capture(self) -> Observed:
        paths = tuple(_run("systemctl", "show", UNIT, "-p", "DropInPaths",
                           "--value").decode("utf-8").split())
        if len(paths) not in (4, 5):
            raise OperationalBlock("dropin_count_unexpected")
        if len(set(paths)) != len(paths):
            raise OperationalBlock("dropin_duplicate")
        dropins = {}
        for raw in paths:
            path = Path(raw)
            if path.parent != self.directory or not re.fullmatch(
                r"[A-Za-z0-9_.-]+\.conf", path.name
            ):
                raise OperationalBlock("dropin_path_unexpected")
            dropins[path.name] = _regular_bytes(path)
        state = _run("systemctl", "is-active", UNIT).decode("ascii").strip()
        return Observed(
            unit_cat=_run("systemctl", "cat", UNIT),
            effective_exec=_run("systemctl", "show", UNIT, "-p", "ExecStart", "--value"),
            paths=paths, dropins=dropins, state=state, health_ok=self.health(),
        )

    def health(self) -> bool:
        connection = http.client.HTTPConnection(HEALTH_HOST, HEALTH_PORT, timeout=3)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            if response.status != 200:
                return False
            body = response.read(65537)
            return len(body) <= 65536 and json.loads(body).get("status") == "ok"
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            return False
        finally:
            connection.close()

    def reload(self) -> None:
        _run("systemctl", "daemon-reload", timeout=20)

    def restart(self) -> None:
        _run("systemctl", "restart", UNIT, timeout=40)


def require_privileged_private_root(root: Path) -> None:
    if os.geteuid() != 0 or root.is_symlink():
        raise OperationalBlock("operator_privilege_unverified")
    try:
        st = root.stat()
    except OSError:
        raise OperationalBlock("private_root_missing") from None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != 0
            or st.st_mode & 0o077):
        raise OperationalBlock("private_root_unsafe")


@contextlib.contextmanager
def exclusive_lock(root: Path):
    fd = os.open(root / ".one-shot.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise OperationalBlock("lock_unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationalBlock("one_shot_already_running") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def rollback_signals():
    """Catch normal termination; defer further signals during restoration."""
    pending = {"restoring": False}
    old = {}

    def handler(signum, _frame):
        if not pending["restoring"]:
            raise Interrupted("operator_signal_received")

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old[sig] = signal.signal(sig, handler)
        yield pending
    finally:
        for sig, previous in old.items():
            signal.signal(sig, previous)


def verify_preflight(before: Observed, *, app: Path, releases: Path,
                     release_sha: str, verified_release_sha: str) -> list[str]:
    if before.state != "active" or not before.health_ok or not before.offline:
        raise OperationalBlock("preflight_runtime_unverified")
    p3.verify_dropin_precedence(list(before.paths))
    if len(before.dropins) != 4 or p3.LATE_DROPIN in before.dropins:
        raise OperationalBlock("preflight_dropins_unverified")
    original, candidate = p3.canonicalize(
        before.effective_exec.decode("utf-8"), app=app, releases=releases,
        release_sha=release_sha, verified_release_sha=verified_release_sha)
    if original == candidate or len(original) != 12 or len(candidate) != 14:
        raise OperationalBlock("preflight_not_migratable")
    return candidate


def verify_switched(after: Observed, candidate: list[str]) -> None:
    if (after.state != "active" or not after.health_ok or not after.offline
            or parse_effective(after.effective_exec) != candidate
            or len(after.dropins) != 5
            or p3.LATE_DROPIN not in after.dropins):
        raise OperationalBlock("switch_runtime_unverified")


def run_once(*, system: LiveSystemd, app: Path, releases: Path,
             release_sha: str, verified_release_sha: str,
             private_root: Path, dropin_dir: Path) -> str:
    """One reversible switch; no direct invocation is exposed.

    Caller must be a separately reviewed immutable privileged wrapper. This
    routine cannot confer authorization. The backup is retained for recovery.
    """
    require_privileged_private_root(private_root)
    with exclusive_lock(private_root):
        before = system.capture()
        candidate = verify_preflight(
            before, app=app, releases=releases, release_sha=release_sha,
            verified_release_sha=verified_release_sha)
        if dropin_dir.is_symlink() or not dropin_dir.is_dir():
            raise OperationalBlock("dropin_directory_unsafe")
        if (dropin_dir / p3.LATE_DROPIN).exists() or (
            dropin_dir / p3.LATE_DROPIN).is_symlink():
            raise OperationalBlock("dropin_already_present")
        # Back up immediately before mutation. Keep the directory on failures.
        private = Path(tempfile.mkdtemp(prefix="p3-", dir=private_root))
        p3.backup_effective_service_snapshot(
            unit_cat=before.unit_cat, dropins=before.dropins,
            effective_exec=before.effective_exec, state=before.state,
            private=private)
        p3.verify_restored_effective_service_snapshot(
            private=private, unit_cat=before.unit_cat, dropins=before.dropins,
            effective_exec=before.effective_exec, state=before.state,
            offline_explicit=before.offline, health_ok=before.health_ok)
        owned = None
        restore_error = None
        try:
            with rollback_signals() as signals:
                try:
                    # A termination between atomic link and returned identity
                    # would leave ownership unknown. Defer catchable signals
                    # until the owned identity has been recorded.
                    guarded = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
                    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, guarded)
                    try:
                        owned = p3.install_late_dropin(
                            dropin_dir, p3.render_dropin(candidate))
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    system.reload()
                    system.restart()
                    verify_switched(system.capture(), candidate)
                finally:
                    signals["restoring"] = True
                    if owned is None:
                        # If installation was interrupted after its atomic
                        # link, ownership is unknown. Never delete blindly.
                        if (dropin_dir / p3.LATE_DROPIN).exists():
                            restore_error = "partial_install_requires_operator"
                    else:
                        if not p3.rollback_late_dropin(dropin_dir, owned):
                            restore_error = "owned_dropin_rollback_failed"
                        else:
                            try:
                                system.reload()
                                system.restart()
                                restored = system.capture()
                                p3.verify_restored_effective_service_snapshot(
                                    private=private, unit_cat=restored.unit_cat,
                                    dropins=restored.dropins,
                                    effective_exec=restored.effective_exec,
                                    state=restored.state,
                                    offline_explicit=restored.offline,
                                    health_ok=restored.health_ok)
                            except Exception:
                                restore_error = "restored_service_unverified"
                    if restore_error:
                        raise OperationalBlock(restore_error)
        except (p3.MigrationBlocked, Interrupted):
            raise
        # No output of systemctl cat, argv, environment, backup path or secrets.
        return "switch_and_restoration_verified"


# Deliberately no __main__, sudo invocation, workflow hook or auto-deploy.
