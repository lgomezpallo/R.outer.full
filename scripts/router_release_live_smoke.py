#!/usr/bin/env python3
"""Live-provider smoke for the frozen Router release; NEVER deploys or edits production.

The outer process creates a transient systemd unit inheriting the existing
service's EnvironmentFile references (not their values). The inner process
copies the configured SQLite DB and catalog privately before importing Router.
All output is allowlisted; prompts, answers, credentials and paths stay private.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCRIPT_SHA = os.environ.get("ROUTER_SCRIPT_SHA", "")
PINNED_SHA = os.environ.get("ROUTER_RELEASE_SHA", "")
APP = Path("/home/ubuntu/Proyecto/Herramientas/Router")
RELEASE = Path.home() / "router-releases" / PINNED_SHA
BACKUPS = Path.home() / "router-private-backups"
VALID_STATUSES = ("live_health=passed", "live_chat=passed", "live_fallback=passed")
SAFE_FALLBACK_DIAGNOSTICS = frozenset({"single_provider", "no_attempts", "first_provider_not_attempted", "no_second_attempt", "second_provider_failed", "unexpected_result"})
ENV_PATTERN = re.compile(r"(-?/[^\s()]+)\s+\(ignore_errors=(?:yes|no)\)")
SAFE_USER = re.compile(r"[a-z_][a-z0-9_-]*")


class SmokeBlocked(Exception):
    pass


def candidate_pin() -> str:
    """Require the exact release SHA supplied by the reviewed workflow."""
    if not re.fullmatch(r"[0-9a-f]{40}", PINNED_SHA):
        raise SmokeBlocked("candidate_sha_required")
    if not re.fullmatch(r"[0-9a-f]{40}", SCRIPT_SHA):
        raise SmokeBlocked("script_sha_required")
    return PINNED_SHA


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def environment_files(raw: str) -> list[str]:
    """Parse systemd's EnvironmentFiles property without revealing its values."""
    if not raw.strip():
        raise SmokeBlocked("service_environment_files_missing")
    entries = ENV_PATTERN.findall(raw)
    residue = ENV_PATTERN.sub("", raw).strip()
    if residue or not entries or len(entries) > 8:
        raise SmokeBlocked("service_environment_layout_unsupported")
    for item in entries:
        if not re.fullmatch(r"-?/[-a-zA-Z0-9_./]+", item) or ".." in Path(item.lstrip("-")).parts:
            raise SmokeBlocked("service_environment_layout_unsupported")
    return entries


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SmokeBlocked("unsafe_private_directory")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def backup_sqlite(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise SmokeBlocked("database_path_unsafe")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=15) as original:
        with sqlite3.connect(target) as clone:
            original.backup(clone)
    target.chmod(0o600)


def internal(work: Path) -> None:
    """Run in a short-lived unit with provider environment, isolated data."""
    candidate_pin()
    if not work.is_dir() or work.is_symlink():
        raise SmokeBlocked("unsafe_private_directory")
    if command("git", "-C", str(RELEASE), "rev-parse", "HEAD") != PINNED_SHA:
        raise SmokeBlocked("frozen_release_mismatch")
    prod_workdir = Path(os.environ.get("ROUTER_PRODUCTION_WORKDIR", str(APP)))
    if prod_workdir != APP:
        raise SmokeBlocked("unexpected_production_workdir")

    # Use the actual effective production data snapshot, not EnvironmentFiles
    # that may omit paths supplied by the active systemd release drop-in.
    source_db = Path(os.environ.get("ROUTER_EFFECTIVE_DB_FILE", ""))
    source_catalog = Path(os.environ.get("ROUTER_EFFECTIVE_CATALOG_FILE", ""))
    if not source_db.is_absolute() or not source_catalog.is_absolute():
        raise SmokeBlocked("effective_data_paths_missing")
    isolated_db = work / "smoke.db"
    backup_sqlite(source_db, isolated_db)
    if source_catalog.is_symlink() or not source_catalog.is_file():
        raise SmokeBlocked("catalog_path_unsafe")
    shutil.copyfile(source_catalog, work / "smoke-catalog.json")
    (work / "smoke-catalog.json").chmod(0o600)

    # MUST override production data paths before importing router.api.
    os.environ["ROUTER_DB_FILE"] = str(isolated_db)
    os.environ["ROUTER_CATALOG_FILE"] = str(work / "smoke-catalog.json")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    # Online access is allowed ONLY inside this isolated short-lived unit.
    # The production router.service stays offline and loopback-only.
    os.environ["ROUTER_OFFLINE_ONLY"] = "0"
    for key in ("ROUTER_STARTUP_PROVIDER_TESTS", "ROUTER_STARTUP_ROUTING_SMOKE",
                "ROUTER_STARTUP_CAPABILITY_PROFILE", "ROUTER_STARTUP_ORCHESTRATION_SMOKE",
                "ROUTER_STARTUP_HETEROGENEOUS_SMOKE"):
        os.environ[key] = "0"
    sys.path.insert(0, str(RELEASE))

    import httpx
    from fastapi.testclient import TestClient
    from router import api
    from router.selector import rank, billing_ineligibility_reason
    from router.types import RouteRequest

    token = os.getenv("ROUTER_SERVICE_TOKEN") or os.getenv("ROUTER_IACHAT_TOKEN")
    headers = {"Authorization": "Bearer " + token} if token else {}
    with TestClient(api.app) as client:
        status = client.get("/health")
        if status.status_code != 200 or status.json().get("status") != "ok":
            raise SmokeBlocked("health_failed")
        print("live_health=passed", flush=True)
        providers = api.router.registry.all()
        if len(providers) < 2:
            raise SmokeBlocked("insufficient_live_providers")
        # Fail closed before ANY real provider request unless every ranked
        # candidate is explicitly free and its endpoint is eligible.
        request = RouteRequest(task="Say hola in one short sentence.", decompose=False,
                               timeout_s=22, application_name="release-smoke")
        decisions = rank(providers, request)
        provider_map = {provider.spec.id: provider for provider in providers}
        if not decisions:
            raise SmokeBlocked("no_explicit_free_candidates")
        for decision in decisions:
            provider = provider_map.get(decision.provider)
            models = ([m for m in provider.spec.models if m.id == decision.model]
                      if provider is not None else [])
            if (len(models) != 1 or models[0].billing_status != "free"
                    or billing_ineligibility_reason(provider, models[0]) is not None):
                raise SmokeBlocked("nonfree_candidate_rejected")
        reply = client.post("/v1/chat/completions", json={
            "model": "router-ia-auto",
            "messages": [{"role": "user", "content": "Reply with a short greeting in Spanish."}],
            "stream": False,
        }, headers=headers)
        if reply.status_code != 200:
            raise SmokeBlocked("live_chat_failed")
        body = reply.json()
        answer = body.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(answer, str) or not answer.strip():
            raise SmokeBlocked("live_chat_empty")
        print("live_chat=passed", flush=True)

        # Re-rank after the live chat: its health/cooldown observations may
        # change candidate order. Inject quota failure into the actual first
        # eligible provider of this separate fallback request.
        fallback_decisions = rank(api.router.registry.all(), request)
        distinct = list(dict.fromkeys(item.provider for item in fallback_decisions))
        if len(distinct) < 2:
            print("live_fallback_diagnostic=single_provider", flush=True)
            raise SmokeBlocked("insufficient_fallback_candidates")
        first = distinct[0]
        real_complete = api.router.client.complete
        def force_first_provider_failure(provider, model, req):
            if provider.spec.id == first:
                fake_req = httpx.Request("POST", "https://invalid.example/")
                fake_resp = httpx.Response(429, request=fake_req)
                raise httpx.HTTPStatusError("injected_provider_quota", request=fake_req, response=fake_resp)
            return real_complete(provider, model, req)
        api.router.client.complete = force_first_provider_failure
        try:
            result = api.router.route(request)
        finally:
            api.router.client.complete = real_complete
        attempted = [item.provider for item in result.attempts]
        if (not result.ok or not result.text or len(attempted) < 2
                or attempted[0] != first or result.provider == first
                or result.provider not in attempted):
            if not attempted:
                reason = "no_attempts"
            elif attempted[0] != first:
                reason = "first_provider_not_attempted"
            elif len(set(attempted)) < 2:
                reason = "no_second_attempt"
            elif not result.ok:
                reason = "second_provider_failed"
            else:
                reason = "unexpected_result"
            print("live_fallback_diagnostic=" + reason, flush=True)
            raise SmokeBlocked("live_fallback_failed")
        print("live_fallback=passed", flush=True)


def orchestrate(script: Path) -> None:
    candidate_pin()
    if command("systemctl", "is-active", "router.service") != "active":
        raise SmokeBlocked("production_not_active")
    if command("git", "-C", str(RELEASE), "rev-parse", "HEAD") != PINNED_SHA:
        raise SmokeBlocked("frozen_release_missing")
    if not (APP / ".venv/bin/python").is_file():
        raise SmokeBlocked("python_venv_missing")
    service_user = command("systemctl", "show", "router.service", "-p", "User", "--value")
    if not SAFE_USER.fullmatch(service_user) or service_user != getpass.getuser():
        raise SmokeBlocked("service_user_mismatch")
    workdir = command("systemctl", "show", "router.service", "-p", "WorkingDirectory", "--value")
    if workdir != str(APP):
        raise SmokeBlocked("service_workdir_mismatch")
    inline = command("systemctl", "show", "router.service", "-p", "Environment", "--value")
    if re.search(r"(?:KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)", inline, flags=re.I):
        raise SmokeBlocked("inline_secret_environment_unsupported")
    envfiles = environment_files(command("systemctl", "show", "router.service",
                                         "-p", "EnvironmentFiles", "--value"))
    # Reuse the tested, read-only effective-path resolver from the pinned release.
    import importlib.util
    promote_path = RELEASE / "scripts/router_release_promote.py"
    spec = importlib.util.spec_from_file_location("smoke_pinned_promote", promote_path)
    if spec is None or spec.loader is None:
        raise SmokeBlocked("effective_path_resolver_missing")
    promote_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promote_module)
    effective_db, effective_catalog = promote_module.effective_live_data_paths()
    private_dir(BACKUPS)
    work = Path(tempfile.mkdtemp(prefix="router-live-smoke-", dir=BACKUPS))
    work.chmod(0o700)
    unit = "router-release-smoke-" + uuid.uuid4().hex[:12]
    args = ["sudo", "-n", "systemd-run", "--quiet", "--pipe", "--wait", "--collect",
            "--unit=" + unit,
            "--property=User=" + service_user,
            "--property=WorkingDirectory=" + str(RELEASE),
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=ReadWritePaths=" + str(work),
            "--property=Environment=ROUTER_PRODUCTION_WORKDIR=" + str(APP),
            "--property=Environment=ROUTER_SCRIPT_SHA=" + SCRIPT_SHA,
            "--setenv=ROUTER_RELEASE_SHA=" + PINNED_SHA,
            "--setenv=ROUTER_EFFECTIVE_DB_FILE=" + effective_db,
            "--setenv=ROUTER_EFFECTIVE_CATALOG_FILE=" + effective_catalog]
    args.extend("--property=EnvironmentFile=" + entry for entry in envfiles)
    args += [str(APP / ".venv/bin/python"), str(script), "internal", str(work)]
    try:
        with (work / "private-output.log").open("wb") as log:
            try:
                result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=240)
            except subprocess.TimeoutExpired:
                raise SmokeBlocked("live_smoke_timeout") from None
        # Strict allowlist: never echo systemd, traceback, prompts or provider output.
        output = (work / "private-output.log").read_text(errors="replace")
        for label in VALID_STATUSES:
            if label in output:
                print(label, flush=True)
        # Export only fixed categorical diagnostics, never private logs,
        # provider names, model names, credentials, paths, prompts or answers.
        for reason in sorted(SAFE_FALLBACK_DIAGNOSTICS):
            if "live_fallback_diagnostic=" + reason in output:
                print("live_fallback_diagnostic=" + reason, flush=True)
        if result.returncode != 0 or not all(label in output for label in VALID_STATUSES):
            raise SmokeBlocked("live_smoke_failed")
        if command("systemctl", "is-active", "router.service") != "active":
            raise SmokeBlocked("production_changed")
        print("production_service_unchanged=yes", flush=True)
        print("release_status=LIVE_SMOKE_PASSED_NOT_DEPLOYED", flush=True)
    finally:
        subprocess.run(["sudo", "-n", "systemctl", "stop", unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "orchestrate":
            orchestrate(Path(__file__).resolve())
        elif len(sys.argv) == 3 and sys.argv[1] == "internal":
            internal(Path(sys.argv[2]))
        else:
            raise SmokeBlocked("invalid_smoke_mode")
    except (SmokeBlocked, OSError, subprocess.SubprocessError, sqlite3.Error,
            ValueError, KeyError, ImportError):
        print("live_smoke_status=blocked", flush=True)
        sys.exit(2)
