"""Stage 8: offline contract between audit progress and audit-step candidates.

No network, probes, provider inference or billing API calls. The sole audit
execution in this test is a local stub returning a synthetic result.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from router.catalog import CapabilityEvidence, ModelSpec, ProviderSpec
from router.registry import RegisteredProvider
from router.state import RuntimeState


def _fixture_provider():
    def model(name, capabilities=("chat",), evidence=()):
        return ModelSpec(
            id=name, capabilities=frozenset(capabilities),
            model_class="standard", evidence=tuple(evidence),
        )

    return RegisteredProvider(
        ProviderSpec("groq", ("groq",), "https://example.invalid/v1", (
            model("fixture-ready"),
            model("fixture-billing-blocked"),
            model("fixture-legacy", evidence=(
                CapabilityEvidence("chat", "verified", "migrated_from_v1", "2026-10-26"),
            )),
            model("fixture-verified", evidence=(
                CapabilityEvidence("chat", "verified", "offline-fixture", "2026-10-26"),
            )),
            model("fixture-not-inferred"),
            model("fixture-not-declared", capabilities=("vision",)),
        )),
        "never-use-this-key", RuntimeState(),
    )


def _install_offline_fixtures(monkeypatch, provider, billing):
    import router.api as api
    import router.catalog as catalog
    import router.selector as selector

    calls = []
    reservations = []

    def fake_verify(provider_id, model_id, capability):
        calls.append((provider_id, model_id, capability))
        return {"model": model_id, "status": "offline-fixture"}

    def fake_reserve(provider_id, model_id, capability, limit):
        reservations.append((provider_id, model_id, capability, limit))
        return "reserved", len(reservations)

    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "offline-admin-token")
    monkeypatch.setattr(catalog, "infer_discovered_capabilities",
                        lambda name: frozenset() if name == "fixture-not-inferred"
                        else frozenset({"chat"}))
    monkeypatch.setattr(selector, "billing_ineligibility_reason", billing)
    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider]),
        verify_capability=fake_verify,
    ))
    monkeypatch.setattr(api, "store", SimpleNamespace(
        audit_budget_used=lambda: {"groq": 0},
        reserve_audit_model=fake_reserve,
    ))
    return api, calls, reservations


def test_progress_actionable_count_matches_step_without_retrying_legacy(monkeypatch):
    provider = _fixture_provider()
    api, calls, reservations = _install_offline_fixtures(
        monkeypatch, provider,
        lambda _provider, model: "billing_unverified"
        if model.id == "fixture-billing-blocked" else None,
    )
    client = TestClient(api.app)
    progress = client.get("/audit/progress")
    assert progress.status_code == 200
    body = progress.json()
    # Five routes lack verified CHAT evidence; only one meets the exact
    # candidate predicate. Legacy evidence is not retried automatically.
    assert body["platforms"]["groq"]["untested_routes"] == 5
    assert body["eligible_pending_chat_audit_candidates"]["groq"] == 1
    assert calls == [] and reservations == []

    response = client.post(
        "/audit/step", json={"provider": "groq", "max_calls": 1},
        headers={"Authorization": "Bearer offline-admin-token"},
    )
    assert response.status_code == 200
    assert response.json()["results"] == [
        {"model": "fixture-ready", "status": "offline-fixture"},
    ]
    assert response.json()["pending_chat"] == 0
    assert calls == [("groq", "fixture-ready", "chat")]
    assert len(reservations) == 1
    assert "never-use-this-key" not in progress.text + response.text


def test_no_account_eligibility_means_zero_actionable_candidates(monkeypatch):
    provider = _fixture_provider()
    api, calls, reservations = _install_offline_fixtures(
        monkeypatch, provider, lambda _provider, _model: "billing_unverified",
    )
    client = TestClient(api.app)
    progress = client.get("/audit/progress").json()
    assert progress["platforms"]["groq"]["untested_routes"] == 5
    assert progress["eligible_pending_chat_audit_candidates"]["groq"] == 0

    response = client.post(
        "/audit/step", json={"provider": "groq", "max_calls": 3},
        headers={"Authorization": "Bearer offline-admin-token"},
    )
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["pending_chat"] == 0
    assert calls == [] and reservations == []
