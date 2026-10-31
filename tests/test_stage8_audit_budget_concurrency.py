"""Offline concurrency regression for conservative SQLite audit reservations.

Only temporary SQLite files and local TestClient calls are used. No provider
credentials, inference APIs, probes, billing endpoints or Oracle access.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from router.storage import RouterStore


def test_atomic_daily_reservations_across_connections(tmp_path):
    """Simultaneous workers must never overbook the internal daily cap."""
    path = tmp_path / "audit-concurrent.db"
    stores = [RouterStore(path=path) for _ in range(4)]
    barrier = Barrier(8)

    def reserve(i):
        if i < 8:
            barrier.wait(timeout=20)
        return stores[i % len(stores)].reserve_audit_budget("groq", 12)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(40)))

    assert sum(allowed for allowed, _ in results) == 12
    assert all(0 <= used <= 12 for _, used in results)
    assert stores[0].audit_budget_used()["groq"] == 12
    assert stores[3].reserve_audit_budget("groq", 12) == (False, 12)


def test_provider_budgets_are_independent_under_contention(tmp_path):
    """A full provider budget must not consume another provider's slots."""
    store = RouterStore(path=tmp_path / "audit-independent.db")

    def reserve(i):
        provider, limit = ("groq", 12) if i % 2 == 0 else ("nvidia", 1)
        allowed, used = store.reserve_audit_budget(provider, limit)
        return provider, allowed, used

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(40)))

    assert sum(ok for p, ok, _ in results if p == "groq") == 12
    assert sum(ok for p, ok, _ in results if p == "nvidia") == 1
    assert store.audit_budget_used()["groq"] == 12
    assert store.audit_budget_used()["nvidia"] == 1


def test_audit_progress_labels_budget_as_internal_snapshot(monkeypatch):
    """Progress diagnostics are not provider quotas or spend authorization."""
    import router.api as api
    from types import SimpleNamespace

    monkeypatch.setattr(
        api, "router",
        SimpleNamespace(registry=SimpleNamespace(all=lambda: [])),
    )
    monkeypatch.setattr(
        api, "store",
        SimpleNamespace(audit_budget_used=lambda: {}),
    )
    response = TestClient(api.app).get("/audit/progress")
    assert response.status_code == 200
    note = response.json()["note"].lower()
    assert "intern" in note
    assert "cuotas oficiales" in note
    assert "instantánea" in note
    assert "no habilita inferencia" in note
