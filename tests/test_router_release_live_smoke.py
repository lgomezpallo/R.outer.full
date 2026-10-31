"""Guards for the frozen-release live-provider smoke (no Oracle access in CI)."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_live_smoke.py"
spec = importlib.util.spec_from_file_location("router_release_live_smoke", MODULE)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_pin_and_private_output_allowlist(monkeypatch):
    monkeypatch.setattr(smoke, "PINNED_SHA", "")
    with pytest.raises(smoke.SmokeBlocked, match="candidate_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "PINNED_SHA", "339898a")
    with pytest.raises(smoke.SmokeBlocked, match="candidate_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "PINNED_SHA", "a" * 40)
    monkeypatch.setattr(smoke, "SCRIPT_SHA", "")
    with pytest.raises(smoke.SmokeBlocked, match="script_sha_required"):
        smoke.candidate_pin()
    monkeypatch.setattr(smoke, "SCRIPT_SHA", "b" * 40)
    assert smoke.candidate_pin() == "a" * 40
    assert smoke.VALID_STATUSES == ("live_health=passed", "live_chat=passed", "live_fallback=passed")
    assert smoke.SAFE_FALLBACK_DIAGNOSTICS == frozenset({
        "single_model", "no_attempts", "first_provider_not_attempted",
        "no_second_attempt", "second_provider_failed", "second_model_failed", "unexpected_result",
    })


def test_environment_file_parser():
    assert smoke.environment_files(
        "/etc/router-providers.env (ignore_errors=no) /etc/iachat-router.env (ignore_errors=yes)"
    ) == ["/etc/router-providers.env", "/etc/iachat-router.env"]
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("")
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("/etc/../secrets (ignore_errors=no)")
    with pytest.raises(smoke.SmokeBlocked):
        smoke.environment_files("garbled data")


def test_private_dir_rejects_symlink(tmp_path):
    actual = tmp_path / "real"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(smoke.SmokeBlocked):
        smoke.private_dir(alias)


def test_sqlite_online_snapshot_does_not_change_source(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE items (name TEXT)")
        db.execute("INSERT INTO items VALUES ('kept')")
    target = tmp_path / "snapshot.db"
    smoke.backup_sqlite(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT name FROM items").fetchone() == ("kept",)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone() == (1,)
    assert target.stat().st_mode & 0o777 == 0o600


def test_live_smoke_cannot_deploy_or_mutate_checkout():
    source = MODULE.read_text()
    assert 'SCRIPT_SHA = os.environ.get("ROUTER_SCRIPT_SHA", "")' in source
    assert 'PINNED_SHA = os.environ.get("ROUTER_RELEASE_SHA", "")' in source
    assert '"--property=Environment=ROUTER_SCRIPT_SHA=" + SCRIPT_SHA' in source
    assert 'os.environ["ROUTER_OFFLINE_ONLY"] = "0"' in source
    assert 'models[0].billing_status != "free"' in source
    assert 'billing_ineligibility_reason(provider, models[0])' in source
    assert 'promote_module.effective_live_data_paths()' in source
    assert "candidate_pin()" in source
    assert "fallback_decisions = rank(api.router.registry.all(), request)" in source
    assert "live_fallback_diagnostic=" in source
    assert "for reason in sorted(SAFE_FALLBACK_DIAGNOSTICS):" in source
    for forbidden in (
        "git reset --hard", "git clean -fd", "systemctl restart router.service",
        "systemctl stop router.service", "git checkout -f",
    ):
        assert forbidden not in source
    assert 'os.environ["ROUTER_DB_FILE"] = str(isolated_db)' in source
    assert 'os.environ["ROUTER_CATALOG_FILE"] = str(work / "smoke-catalog.json")' in source


def test_fallback_plan_distinguishes_provider_quota_from_model_failure():
    from router.types import Decision
    one = [Decision("openrouter", "free-a", 100)]
    same_provider = [*one, Decision("openrouter", "free-b", 90)]
    distinct = [*same_provider, Decision("groq", "free-c", 80)]
    assert smoke.fallback_plan(one) is None
    assert smoke.fallback_plan(same_provider) == ("model", "openrouter", "free-a")
    assert smoke.fallback_plan(distinct) == ("provider", "openrouter", "free-a")


def test_fallback_success_requires_distinct_model_or_provider():
    from router.types import Attempt, RouteResponse
    from router.types import Decision
    first = Attempt("openrouter", "free-a", False, 1)
    same = Attempt("openrouter", "free-b", True, 1)
    other = Attempt("groq", "free-c", True, 1)
    def result(second, provider, model):
        return RouteResponse(True, "offline", provider, model, [first, second], [])
    assert smoke.fallback_succeeded(result(same, "openrouter", "free-b"), "model", "openrouter", "free-a")
    assert not smoke.fallback_succeeded(result(same, "openrouter", "free-b"), "provider", "openrouter", "free-a")
    assert smoke.fallback_succeeded(result(other, "groq", "free-c"), "provider", "openrouter", "free-a")
    assert not smoke.fallback_succeeded(result(other, "groq", "free-c"), "model", "openrouter", "free-a")
    assert not smoke.fallback_succeeded(result(first, "openrouter", "free-a"), "model", "openrouter", "free-a")


def test_smoke_uses_500_for_model_scope_and_429_for_provider_scope():
    source = MODULE.read_text()
    assert '429 if scope == "provider" else 500' in source
    assert 'live_fallback_scope=' in source
    assert 'for scope in ("provider", "model"):' in source
    assert 'for key in ("live_free_pool", "live_fallback_pool"):' in source
    assert 'scope == "provider" or model == first_model' in source
    assert 'fallback_decisions = rank(api.router.registry.all(), request)' in source


def test_free_candidate_pool_reports_only_safe_categories():
    from router.types import Decision
    first = Decision("openrouter", "free-a", 100)
    assert smoke.eligible_pool_category([]) == "none"
    assert smoke.eligible_pool_category([first]) == "one_model"
    assert smoke.eligible_pool_category([first, Decision("openrouter", "free-b", 90)]) == "multiple_models_one_provider"
    assert smoke.eligible_pool_category([first, Decision("groq", "free-c", 80)]) == "multiple_providers"
    assert set(smoke.SAFE_POOL_CATEGORIES) == {
        "none", "one_model", "multiple_models_one_provider", "multiple_providers",
    }


def test_live_smoke_auth_preflight_rejects_missing_or_unsafe_auth(monkeypatch):
    """No credentials or provider calls: only the authentication dependency."""
    from types import SimpleNamespace
    from fastapi import HTTPException

    monkeypatch.delenv("ROUTER_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ROUTER_IACHAT_TOKEN", raising=False)
    accepts_anonymous = SimpleNamespace(require_auth=lambda authorization: "development")
    with pytest.raises(smoke.SmokeBlocked, match="router_auth_token_missing"):
        smoke.authenticated_smoke_token(accepts_anonymous)

    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "offline-fixture-token")
    with pytest.raises(smoke.SmokeBlocked, match="router_anonymous_auth_unsafe"):
        smoke.authenticated_smoke_token(accepts_anonymous)

    def authenticates_only_token(authorization):
        if authorization is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        assert authorization == "Bearer offline-fixture-token"
        return "iachat"

    assert smoke.authenticated_smoke_token(
        SimpleNamespace(require_auth=authenticates_only_token)
    ) == "offline-fixture-token"

    def rejects_all_tokens(authorization):
        raise HTTPException(status_code=401, detail="unauthorized")

    with pytest.raises(smoke.SmokeBlocked, match="router_token_auth_failed"):
        smoke.authenticated_smoke_token(
            SimpleNamespace(require_auth=rejects_all_tokens)
        )

    def unexpected_rejection(authorization):
        raise HTTPException(status_code=403, detail="unexpected")

    with pytest.raises(smoke.SmokeBlocked, match="router_anonymous_auth_unsafe"):
        smoke.authenticated_smoke_token(
            SimpleNamespace(require_auth=unexpected_rejection)
        )

    # Authentication must be checked before TestClient starts provider discovery.
    source = MODULE.read_text()
    assert source.index("token = authenticated_smoke_token(api)") < source.index(
        "with TestClient(api.app) as client:"
    )
