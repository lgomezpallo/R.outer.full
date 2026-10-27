"""Offline regression: /audit/progress reports CHAT evidence, not other skills.

Synthetic catalog only. Never calls provider APIs, inference, probes, or billing.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from router.catalog import CapabilityEvidence, ModelSpec, ProviderSpec
from router.registry import RegisteredProvider
from router.state import RuntimeState


def test_audit_progress_uses_chat_evidence_and_excludes_conflicts(monkeypatch):
    import router.api as api

    def evidence(capability, status, source="offline-fixture", checked="2026-10-26T18:00:00Z"):
        return CapabilityEvidence(capability, status, source, checked)

    def model(name, records=()):
        return ModelSpec(
            id=name, capabilities=frozenset({"chat"}), model_class="standard",
            evidence=tuple(records),
        )

    models = (
        model("verified-chat", (evidence("chat", "verified"),)),
        model("verified-vision-only", (evidence("vision", "verified"),)),
        model("legacy-chat", (evidence("chat", "verified", "migrated_from_v1"),)),
        model("inconclusive-chat", (evidence("chat", "inconclusive"),)),
        model("contradictory-chat", (
            evidence("chat", "verified"), evidence("chat", "unsupported"),
        )),
        model("unsupported-chat", (evidence("chat", "unsupported"),)),
        model("no-evidence"),
    )
    provider = RegisteredProvider(
        ProviderSpec("groq", ("groq",), "https://example.invalid/v1", models),
        "offline-key-never-used", RuntimeState(),
    )

    # A bare router registry and store prevent accidental client/probe access.
    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider]),
    ))
    monkeypatch.setattr(api, "store", SimpleNamespace(
        audit_budget_used=lambda: {"groq": 0},
    ))

    response = TestClient(api.app).get("/audit/progress")
    assert response.status_code == 200
    report = response.json()
    counts = report["platforms"]["groq"]
    assert counts == {
        "total_routes": 7,
        "verified_routes": 1,
        "inconclusive_routes": 1,
        "untested_routes": 3,
        "unsupported_or_conflicting_routes": 2,
    }
    assert sum(value for key, value in counts.items() if key != "total_routes") == 7
    assert report["daily_budget"]["groq"]["used"] == 0
    assert "offline-key-never-used" not in response.text


def test_audit_progress_does_not_promote_incomplete_chat_evidence(monkeypatch):
    import router.api as api

    model = ModelSpec(
        id="incomplete-chat", capabilities=frozenset({"chat"}),
        model_class="standard",
        evidence=(CapabilityEvidence("chat", "verified", "", ""),),
    )
    provider = RegisteredProvider(
        ProviderSpec("groq", ("groq",), "https://example.invalid/v1", (model,)),
        "offline-key-never-used", RuntimeState(),
    )
    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider]),
    ))
    monkeypatch.setattr(api, "store", SimpleNamespace(audit_budget_used=lambda: {}))
    counts = TestClient(api.app).get("/audit/progress").json()["platforms"]["groq"]
    assert counts["verified_routes"] == 0
    assert counts["untested_routes"] == 1
