#!/usr/bin/env python3
"""Reversible promotion of the pinned Router release, never a git reset/clean.

Retains the production WorkingDirectory and EnvironmentFiles; only appends
uvicorn --app-dir for the pinned worktree via one dedicated systemd drop-in.
Backs up the effective unit and live SQLite with a private transient unit.
Any failure after the switch restores the original ExecStart automatically.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

PINNED_SHA = os.environ.get("ROUTER_RELEASE_SHA", "")
APP = Path("/home/ubuntu/Proyecto/Herramientas/Router")
RELEASE = Path.home() / "router-releases" / PINNED_SHA
BACKUPS = Path.home() / "router-private-backups"
OVERRIDE = Path("/etc/systemd/system/router.service.d/99-router-frozen-release.conf")
ENV_PATTERN = re.compile(r"(-?/[^\s()]+)\s+\(ignore_errors=(?:yes|no)\)")
SAFE_USER = re.compile(r"[a-z_][a-z0-9_-]*")


class PromoteBlocked(Exception):
    pass


def candidate_pin() -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", PINNED_SHA):
        raise PromoteBlocked("candidate_sha_required")
    return PINNED_SHA


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def run(*args: str) -> None:
    subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=35)


def health() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8010/health", timeout=3) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError, KeyError):
        return False


def wait_health() -> bool:
    for _ in range(25):
        if subprocess.run(["systemctl", "is-active", "--quiet", "router.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0 and health():
            return True
        time.sleep(1)
    return False


def environment_files(raw: str) -> list[str]:
    paths = ENV_PATTERN.findall(raw)
    if not paths or len(paths) > 8 or ENV_PATTERN.sub("", raw).strip():
        raise PromoteBlocked("service_environment_unsupported")
    if any(not re.fullmatch(r"-?/[-a-zA-Z0-9_./]+", path)
           or ".." in Path(path.lstrip("-")).parts for path in paths):
        raise PromoteBlocked("service_environment_unsupported")
    return paths


def original_argv(raw: str) -> list[str]:
    match = re.search(r"argv\[\]=(.*?)\s+;\s+ignore_errors=", raw)
    if not match:
        raise PromoteBlocked("service_exec_layout_unsupported")
    argv = shlex.split(match.group(1))
    if not argv or any(not re.fullmatch(r"[-a-zA-Z0-9_./:=]+", x) for x in argv):
        raise PromoteBlocked("service_exec_layout_unsupported")
    if not str(Path(argv[0])).startswith(str(APP / ".venv/bin/")):
        raise PromoteBlocked("service_interpreter_unsupported")
    if "router.api:app" not in argv or "--reload" in argv or argv.count("--app-dir") > 1:
        raise PromoteBlocked("service_exec_layout_unsupported")
    if "--app-dir" in argv:
        pos = argv.index("--app-dir")
        # Only the current checkout (or its WorkingDirectory '.') is allowed.
        # A different app directory may shadow the pinned Router package.
        if pos + 1 >= len(argv) or argv[pos + 1] not in (".", str(APP)):
            raise PromoteBlocked("service_app_dir_unsupported")
    try:
        if argv[argv.index("--host") + 1] != "127.0.0.1":
            raise PromoteBlocked("service_host_unsupported")
        if argv[argv.index("--port") + 1] != "8010":
            raise PromoteBlocked("service_port_unsupported")
    except (ValueError, IndexError):
        raise PromoteBlocked("service_exec_layout_unsupported") from None
    return argv


def candidate_argv(argv: list[str]) -> list[str]:
    """Replace an existing uvicorn --app-dir instead of duplicating the flag."""
    if "--app-dir" not in argv:
        new_argv = argv + ["--app-dir", str(RELEASE)]
    else:
        new_argv = argv.copy()
        new_argv[new_argv.index("--app-dir") + 1] = str(RELEASE)
    return new_argv


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise PromoteBlocked("unsafe_private_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def online_snapshot(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise PromoteBlocked("database_path_unsafe")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=20) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    destination.chmod(0o600)


def inner_backup(work: Path) -> None:
    db = Path(os.getenv("ROUTER_DB_FILE", "router.db"))
    if not db.is_absolute():
        db = APP / db
    if not db.exists():
        raise PromoteBlocked("database_missing")
    online_snapshot(db, work / "predeploy-router.db")
    catalog = Path(os.getenv("ROUTER_CATALOG_FILE", "router-catalog.json"))
    if not catalog.is_absolute():
        catalog = APP / catalog
    if catalog.is_symlink() or not catalog.is_file():
        raise PromoteBlocked("catalog_path_unsafe_or_missing")
    shutil.copyfile(catalog, work / "predeploy-catalog.json")
    (work / "predeploy-catalog.json").chmod(0o600)
    print("production_data_snapshot=passed", flush=True)


def prepare_candidate_data(work: Path) -> tuple[Path, Path]:
    """Run the candidate on private snapshots, never the live Router DB/catalog.

    Rolling back the systemd drop-in then restores the original code AND data
    paths, without trying to overwrite an active WAL-mode database. Candidate
    writes remain quarantined until a separately approved data migration.
    """
    sources = (work / "predeploy-router.db", work / "predeploy-catalog.json")
    targets = (work / "candidate-router.db", work / "candidate-catalog.json")
    for source, target in zip(sources, targets):
        if (source.is_symlink() or not source.is_file()
                or target.exists() or target.is_symlink()):
            raise PromoteBlocked("candidate_data_snapshot_unavailable")
        shutil.copyfile(source, target)
        target.chmod(0o600)
    with sqlite3.connect(targets[0]) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PromoteBlocked("candidate_database_integrity_failed")
    print("candidate_data_isolated=passed", flush=True)
    return targets


def inner_verify() -> None:
    """Assert a fail-closed G.O. request; never send a real chat completion."""
    token = os.getenv("ROUTER_SERVICE_TOKEN") or os.getenv("ROUTER_IACHAT_TOKEN")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps({
        "task": "Offline release gate for G.O.",
        "conversation": [{"role": "user", "content": "health check only"}],
        "required_capabilities": ["chat"],
        "application_name": "go-ute",
    }).encode()
    req = urllib.request.Request("http://127.0.0.1:8010/route",
                                 data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            body = json.load(response)
            if (response.status != 200 or body.get("ok") is not False
                    or body.get("error") != "no_eligible_provider"
                    or body.get("text") is not None or body.get("attempts") != []):
                raise PromoteBlocked("postdeploy_offline_route_failed")
    except (OSError, ValueError, KeyError):
        raise PromoteBlocked("postdeploy_offline_route_failed") from None
    print("postdeploy_offline_route=passed", flush=True)


def private_unit(script: Path, work: Path, user: str, envfiles: list[str], mode: str) -> None:
    unit = "router-promote-check-" + uuid.uuid4().hex[:12]
    args = ["sudo", "-n", "systemd-run", "--quiet", "--pipe", "--wait", "--collect",
            "--unit=" + unit, "--property=User=" + user,
            "--property=WorkingDirectory=" + str(APP),
            "--property=NoNewPrivileges=yes", "--property=ProtectSystem=strict",
            "--property=ReadWritePaths=" + str(work)]
    args.extend("--property=EnvironmentFile=" + path for path in envfiles)
    args += [str(APP / ".venv/bin/python"), str(script), mode, str(work)]
    try:
        with (work / ("private-" + mode + ".log")).open("wb") as log:
            try:
                result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=130)
            except subprocess.TimeoutExpired:
                raise PromoteBlocked("private_unit_timeout") from None
        expected = "production_data_snapshot=passed" if mode == "backup" else "postdeploy_offline_route=passed"
        if result.returncode != 0 or expected not in (work / ("private-" + mode + ".log")).read_text(errors="replace"):
            raise PromoteBlocked("private_" + mode + "_failed")
        print(expected, flush=True)
    finally:
        subprocess.run(["sudo", "-n", "systemctl", "stop", unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def pinned_process() -> bool:
    try:
        pid = int(command("systemctl", "show", "router.service", "-p", "MainPID", "--value"))
        if pid <= 0:
            return False
        argv = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        text = [item.decode(errors="replace") for item in argv if item]
        return "--app-dir" in text and text[text.index("--app-dir") + 1] == str(RELEASE)
    except (ValueError, IndexError, OSError):
        return False


def rollback() -> bool:
    try:
        run("sudo", "-n", "rm", "-f", str(OVERRIDE))
        run("sudo", "-n", "systemctl", "daemon-reload")
        run("sudo", "-n", "systemctl", "restart", "router.service")
        return wait_health()
    except (OSError, subprocess.SubprocessError):
        return False


def promote(script: Path) -> None:
    candidate_pin()
    if command("git", "-C", str(RELEASE), "rev-parse", "HEAD") != PINNED_SHA:
        raise PromoteBlocked("release_pin_mismatch")
    if not wait_health():
        raise PromoteBlocked("production_preflight_unhealthy")
    if command("systemctl", "show", "router.service", "-p", "WorkingDirectory", "--value") != str(APP):
        raise PromoteBlocked("service_workdir_unsupported")
    user = command("systemctl", "show", "router.service", "-p", "User", "--value")
    if not SAFE_USER.fullmatch(user) or user != getpass.getuser():
        raise PromoteBlocked("service_user_unsupported")
    envfiles = environment_files(command("systemctl", "show", "router.service",
                                         "-p", "EnvironmentFiles", "--value"))
    inline = command("systemctl", "show", "router.service", "-p", "Environment", "--value")
    if re.search(r"(?:KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)", inline, re.I):
        raise PromoteBlocked("inline_secret_environment_unsupported")
    argv = original_argv(command("systemctl", "show", "router.service", "-p", "ExecStart", "--value"))
    if subprocess.run(["sudo", "-n", "test", "-e", str(OVERRIDE)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        raise PromoteBlocked("existing_release_override")
    private_dir(BACKUPS)
    work = Path(tempfile.mkdtemp(prefix="router-promote-", dir=BACKUPS))
    work.chmod(0o700)
    with (work / "original-unit.txt").open("wb") as file:
        subprocess.run(["systemctl", "cat", "router.service"], stdout=file,
                       stderr=subprocess.DEVNULL, check=True)
    (work / "original-unit.txt").chmod(0o600)
    private_unit(script, work, user, envfiles, "backup")
    candidate_db, candidate_catalog = prepare_candidate_data(work)
    # Retain WorkingDirectory, EnvironmentFiles and the exact existing CLI flags.
    # The executable's environment overrides the original live-data paths.
    # On rollback, removing this drop-in restores the untouched live DB/catalog.
    new_argv = candidate_argv(argv)
    content = ("[Service]\nIPAddressDeny=any\nIPAddressAllow=localhost\n"
               "ExecStart=\nExecStart=/usr/bin/env ROUTER_OFFLINE_ONLY=1 "
               + "ROUTER_DB_FILE=" + str(candidate_db) + " "
               + "ROUTER_CATALOG_FILE=" + str(candidate_catalog) + " "
               + " ".join(new_argv) + "\n")
    staged = work / "release-override.conf"
    staged.write_text(content)
    staged.chmod(0o600)
    installed = False
    try:
        run("sudo", "-n", "mkdir", "-p", str(OVERRIDE.parent))
        temp_override = str(OVERRIDE) + ".pending-" + uuid.uuid4().hex[:10]
        run("sudo", "-n", "install", "-m", "0644", str(staged), temp_override)
        run("sudo", "-n", "mv", "-T", temp_override, str(OVERRIDE))
        installed = True
        run("sudo", "-n", "systemctl", "daemon-reload")
        run("sudo", "-n", "systemctl", "restart", "router.service")
        if not wait_health() or not pinned_process():
            raise PromoteBlocked("postdeploy_health_or_pin_failed")
        print("postdeploy_health=passed", flush=True)
        print("postdeploy_release_pin=passed", flush=True)
        private_unit(script, work, user, envfiles, "verify")
        print("production_release_sha=" + PINNED_SHA, flush=True)
        print("release_status=DEPLOYED_VERIFIED", flush=True)
        installed = False  # Successful promotion: keep our reversible drop-in.
    finally:
        if installed:
            restored = rollback()
            print("automatic_rollback=" + ("passed" if restored else "failed"), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "promote":
            promote(Path(__file__).resolve())
        elif len(sys.argv) == 3 and sys.argv[1] in ("backup", "verify"):
            if not Path(sys.argv[2]).is_dir():
                raise PromoteBlocked("private_directory_missing")
            if sys.argv[1] == "backup":
                inner_backup(Path(sys.argv[2]))
            else:
                inner_verify()
        else:
            raise PromoteBlocked("invalid_mode")
    except (PromoteBlocked, OSError, subprocess.SubprocessError, sqlite3.Error,
            ValueError, KeyError):
        print("promotion_status=blocked", flush=True)
        sys.exit(2)
