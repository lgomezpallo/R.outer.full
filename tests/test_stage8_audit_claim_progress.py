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
