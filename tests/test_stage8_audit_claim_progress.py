"""Stage 8 offline regression: durable audit claims are not pending work.

Only synthetic catalog records, temporary SQLite and a mocked verifier.
No provider HTTP, inference probes, billing access, or deployments.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from router.catalog import ModelSpec
from router.storage import RouterStore


def _offline_api(monkeypatch, path):
    import router.api as api

    models = tuple(
        ModelSpec(id=name, capabilities=frozenset({"chat"}), model_class="standard")
        for name in ("synthetic-a", "synthetic-b", "synthetic-c")
    )
    provider = SimpleNamespace(spec=SimpleNamespace(id="groq", models=models))
    calls = []

    def mocked_verify(provider_id, model_id, capability):
        calls.append((provider_id, model_id, capability))
        return {"model": model_id, "status": "offline-stub"}

    monkeypatch.setattr(api, "store", RouterStore(path=path))
    monkeypatch.setattr(
        api, "router",
        SimpleNamespace(
            registry=SimpleNamespace(all=lambda: [provider]),
            verify_capability=mocked_verify,
        ),
    )
    # This test isolates the claim accounting, not catalog or billing policy.
    monkeypatch.setattr(api, "_chat_audit_candidates", lambda p: list(p.spec.models))
    return api, calls


def test_progress_excludes_durable_chat_claims_without_reserving(monkeypatch, tmp_path):
    api, calls = _offline_api(monkeypatch, tmp_path / "claims-progress.db")
    assert api.store.reserve_audit_model("groq", "synthetic-a", "chat", 12) == ("reserved", 1)
    # A vision claim must not exclude the same model from a chat audit.
    assert api.store.reserve_audit_model("groq", "synthetic-b", "vision", 12) == ("reserved", 2)

    response = TestClient(api.app).get("/audit/progress")
    assert response.status_code == 200
    body = response.json()
    assert body["eligible_pending_chat_audit_candidates"]["groq"] == 2
    assert body["budget_limited_pending_chat_candidates"]["groq"] == 2
    assert body["daily_budget"]["groq"] == {"limit": 12, "used": 2}
    assert "persisten" in body["note"]
    assert calls == []
    assert api.store.audit_budget_used()["groq"] == 2


def test_progress_stays_excluded_after_reopening_sqlite(monkeypatch, tmp_path):
    path = tmp_path / "claims-restart.db"
    api, calls = _offline_api(monkeypatch, path)
    api.store.reserve_audit_model("groq", "synthetic-a", "chat", 12)
    api.store.reserve_audit_model("groq", "synthetic-b", "chat", 12)
    monkeypatch.setattr(api, "store", RouterStore(path=path))
    body = TestClient(api.app).get("/audit/progress").json()
    assert body["eligible_pending_chat_audit_candidates"]["groq"] == 1
    assert body["budget_limited_pending_chat_candidates"]["groq"] == 1
    assert calls == []


def test_audit_step_skips_prior_claims_and_progress_reaches_zero(monkeypatch, tmp_path):
    api, calls = _offline_api(monkeypatch, tmp_path / "claims-step.db")
    api.store.reserve_audit_model("groq", "synthetic-a", "chat", 12)
    result = api.audit_step(api.AuditStepInput(provider="groq", max_calls=2))
    assert [item["model"] for item in result["results"]] == [
        "synthetic-b", "synthetic-c",
    ]
    assert result["skipped_claimed"] == 1
    assert result["pending_chat"] == 0
    assert len(calls) == 2
    assert api.store.audit_budget_used()["groq"] == 3
    body = TestClient(api.app).get("/audit/progress").json()
    assert body["eligible_pending_chat_audit_candidates"]["groq"] == 0
    assert body["budget_limited_pending_chat_candidates"]["groq"] == 0


def test_retired_catalog_model_claim_survives_removal_and_return(monkeypatch, tmp_path):
    """Catalog churn must not reset a durable claim or trigger another probe."""
    api, calls = _offline_api(monkeypatch, tmp_path / "catalog-churn.db")
    provider = api.router.registry.all()[0]
    original = provider.spec.models
    assert api.store.reserve_audit_model("groq", "synthetic-a", "chat", 12) == ("reserved", 1)

    # Simulate a successful inventory refresh dropping an already-claimed ID.
    provider.spec.models = tuple(m for m in original if m.id != "synthetic-a")
    retired = TestClient(api.app).get("/audit/progress").json()
    assert retired["retired_or_missing_chat_audit_claims"]["groq"] == 1
    assert retired["eligible_pending_chat_audit_candidates"]["groq"] == 2
    assert calls == []

    result = api.audit_step(api.AuditStepInput(provider="groq", max_calls=2))
    assert {item["model"] for item in result["results"]} == {"synthetic-b", "synthetic-c"}
    assert result["pending_chat"] == 0
    assert len(calls) == 2

    # Reintroducing the identical ID must not silently permit re-auditing.
    provider.spec.models = original
    restored = TestClient(api.app).get("/audit/progress").json()
    assert restored["retired_or_missing_chat_audit_claims"]["groq"] == 0
    assert restored["eligible_pending_chat_audit_candidates"]["groq"] == 0
    retry = api.audit_step(api.AuditStepInput(provider="groq", max_calls=3))
    assert retry["results"] == []
    assert retry["skipped_claimed"] == 3
    assert retry["pending_chat"] == 0
    assert len(calls) == 2
    assert api.store.audit_budget_used()["groq"] == 3


def test_pending_chat_excludes_claims_beyond_max_calls(monkeypatch, tmp_path):
    """A claimed candidate after the first result is not pending work."""
    api, calls = _offline_api(monkeypatch, tmp_path / "unvisited-claims.db")
    assert api.store.reserve_audit_model("groq", "synthetic-c", "chat", 12) == ("reserved", 1)
    result = api.audit_step(api.AuditStepInput(provider="groq", max_calls=1))
    assert [item["model"] for item in result["results"]] == ["synthetic-a"]
    assert result["skipped_claimed"] == 0  # c was not visited
    assert result["pending_chat"] == 1  # only b, never claimed c
    assert calls == [("groq", "synthetic-a", "chat")]
    progress = TestClient(api.app).get("/audit/progress").json()
    assert progress["eligible_pending_chat_audit_candidates"]["groq"] == 1


def test_claims_are_scoped_by_capability_and_provider(monkeypatch, tmp_path):
    api, calls = _offline_api(monkeypatch, tmp_path / "claim-scope.db")
    store = api.store
    store.reserve_audit_model("groq", "synthetic-a", "vision", 12)
    store.reserve_audit_model("nvidia", "synthetic-a", "chat", 12)
    assert store.audit_claimed_models("groq", "chat") == set()
    assert store.audit_claimed_models("groq", "vision") == {"synthetic-a"}
    assert store.audit_claimed_models("nvidia", "chat") == {"synthetic-a"}
    progress = TestClient(api.app).get("/audit/progress").json()
    assert progress["retired_or_missing_chat_audit_claims"]["groq"] == 0
    assert progress["eligible_pending_chat_audit_candidates"]["groq"] == 3
    assert calls == []
