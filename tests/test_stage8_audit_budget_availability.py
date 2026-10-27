"""Offline regression: audit progress must not overstate remaining daily slots.

All providers and budgets are synthetic. This module never calls an external
inference, discovery, probe, billing API, or Oracle endpoint.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    ("used", "expected"),
    [(0, 3), (10, 2), (11, 1), (12, 0), (17, 0)],
)
def test_audit_progress_caps_pending_candidates_by_budget(monkeypatch, used, expected):
    import router.api as api

    provider = SimpleNamespace(spec=SimpleNamespace(id="groq", models=()))

    def unexpected_external_operation(*_args, **_kwargs):
        raise AssertionError("progress must not execute or reserve a probe")

    monkeypatch.setattr(
        api, "router", SimpleNamespace(
            registry=SimpleNamespace(all=lambda: [provider]),
            verify_capability=unexpected_external_operation,
        ),
    )
    monkeypatch.setattr(
        api, "store", SimpleNamespace(
            audit_budget_used=lambda: {"groq": used},
            reserve_audit_budget=unexpected_external_operation,
        ),
    )
    monkeypatch.setattr(
        api, "_chat_audit_candidates", lambda _provider: [object(), object(), object()],
    )

    response = TestClient(api.app).get("/audit/progress")
    assert response.status_code == 200
    progress = response.json()
    assert progress["eligible_pending_chat_audit_candidates"]["groq"] == 3
    assert progress["budget_limited_pending_chat_candidates"]["groq"] == expected
    assert progress["daily_budget"]["groq"] == {"limit": 12, "used": used}
    assert progress["platforms"]["groq"]["total_routes"] == 0


def test_audit_progress_no_registered_provider_has_no_candidate_count(monkeypatch):
    import router.api as api

    monkeypatch.setattr(
        api, "router", SimpleNamespace(registry=SimpleNamespace(all=lambda: [])),
    )
    monkeypatch.setattr(
        api, "store", SimpleNamespace(audit_budget_used=lambda: {"groq": 12}),
    )
    response = TestClient(api.app).get("/audit/progress")
    assert response.status_code == 200
    assert response.json()["budget_limited_pending_chat_candidates"] == {}
