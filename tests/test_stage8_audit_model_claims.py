"""Stage 8: deterministic offline regressions for per-model audit claims.

Only temporary SQLite and mocked Python functions are used. No network,
provider credentials, external probes, billing, Oracle or deployment.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from router.storage import RouterStore


def test_same_model_is_claimed_once_across_connections(tmp_path):
    path = tmp_path / "model-claims.db"
    stores = [RouterStore(path=path) for _ in range(4)]
    barrier = Barrier(8)

    def claim(i):
        if i < 8:
            barrier.wait(timeout=20)
        return stores[i % 4].reserve_audit_model("groq", "synthetic-one", "chat", 12)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(40)))

    assert [status for status, _ in results].count("reserved") == 1
    assert [status for status, _ in results].count("already_claimed") == 39
    assert stores[0].audit_budget_used()["groq"] == 1
    assert RouterStore(path=path).reserve_audit_model(
        "groq", "synthetic-one", "chat", 12
    ) == ("already_claimed", 1)


def test_exhausted_budget_does_not_claim_unattempted_model(tmp_path):
    store = RouterStore(path=tmp_path / "claims-budget.db")
    assert store.reserve_audit_model("groq", "a", "chat", 1) == ("reserved", 1)
    assert store.reserve_audit_model("groq", "b", "chat", 1) == ("budget_exhausted", 1)
    # Test only the storage transaction; no inference is made.
    assert store.reserve_audit_model("groq", "b", "chat", 2) == ("reserved", 2)
    assert store.reserve_audit_model("groq", "a", "vision", 3) == ("reserved", 3)
    assert store.reserve_audit_model("nvidia", "a", "chat", 1) == ("reserved", 1)


def test_concurrent_audit_steps_select_distinct_models_offline(monkeypatch, tmp_path):
    import router.api as api

    models = [
        SimpleNamespace(id=name, capabilities=frozenset({"chat"}), strategic_cost=1)
        for name in ("synthetic-a", "synthetic-b")
    ]
    provider = SimpleNamespace(spec=SimpleNamespace(id="groq", models=models))
    barrier = Barrier(2)
    lock = Lock()
    calls = []

    def candidates(_provider):
        barrier.wait(timeout=20)
        return list(models)

    def fake_verify(provider_id, model_id, capability):
        with lock:
            calls.append((provider_id, model_id, capability))
        return {"provider": provider_id, "model": model_id, "status": "verified"}

    monkeypatch.setattr(api, "store", RouterStore(path=tmp_path / "api-claims.db"))
    monkeypatch.setattr(
        api, "router",
        SimpleNamespace(
            registry=SimpleNamespace(all=lambda: [provider]),
            verify_capability=fake_verify,
        ),
    )
    monkeypatch.setattr(api, "_chat_audit_candidates", candidates)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: api.audit_step(api.AuditStepInput(provider="groq", max_calls=1)),
            range(2),
        ))

    assert len(calls) == 2
    assert {model_id for _, model_id, _ in calls} == {"synthetic-a", "synthetic-b"}
    assert sum(len(item["results"]) for item in results) == 2
    assert api.store.audit_budget_used()["groq"] == 2
    assert sum(item["skipped_claimed"] for item in results) == 1


def test_failed_probe_claim_remains_durable_and_cannot_retry(monkeypatch, tmp_path):
    import router.api as api

    model = SimpleNamespace(
        id="synthetic-failure", capabilities=frozenset({"chat"}), strategic_cost=1
    )
    provider = SimpleNamespace(spec=SimpleNamespace(id="groq", models=[model]))
    calls = []

    def fake_verify(*args):
        calls.append(args)
        raise RuntimeError("synthetic_probe_failure")

    monkeypatch.setattr(api, "store", RouterStore(path=tmp_path / "failed-claim.db"))
    monkeypatch.setattr(
        api, "router",
        SimpleNamespace(
            registry=SimpleNamespace(all=lambda: [provider]),
            verify_capability=fake_verify,
        ),
    )
    monkeypatch.setattr(api, "_chat_audit_candidates", lambda _: [model])

    with pytest.raises(RuntimeError, match="synthetic_probe_failure"):
        api.audit_step(api.AuditStepInput(provider="groq"))
    retry = api.audit_step(api.AuditStepInput(provider="groq"))
    assert retry["results"] == []
    assert retry["skipped_claimed"] == 1
    assert len(calls) == 1
    assert api.store.audit_budget_used()["groq"] == 1
