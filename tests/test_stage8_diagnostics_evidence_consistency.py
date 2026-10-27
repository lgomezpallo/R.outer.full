"""Stage 8: offline consistency of evidence, coverage and admin ranking previews.

All provider data is synthetic and non-routable. No discovery, probe, inference,
billing API or Oracle access is permitted or needed.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from router.catalog import CapabilityEvidence, ModelSpec, ProviderSpec
from router.registry import RegisteredProvider
from router.selector import model_coverage, rank
from router.state import RuntimeState
from router.types import RouteRequest

pytestmark = pytest.mark.usefixtures("allow_offline_selector_fixture")


def _provider():
    checked = "2026-10-26T18:00:00Z"

    def model(name, records):
        return ModelSpec(
            id=name,
            capabilities=frozenset({"chat"}),
            model_class="standard",
            evidence=tuple(
                CapabilityEvidence("chat", status, source, checked)
                for status, source in records
            ),
        )

    models = (
        model("chat-legacy", [("verified", "migrated_from_v1")]),
        model("chat-fresh", [("verified", "offline-fixture")]),
        model("chat-inconclusive", [("inconclusive", "offline-fixture")]),
        model("chat-conflicting", [
            ("verified", "offline-fixture"),
            ("unsupported", "offline-fixture"),
        ]),
    )
    return RegisteredProvider(
        ProviderSpec("fixture", ("fixture",), "https://example.invalid/v1", models),
        "synthetic-key-never-used",
        RuntimeState(),
    )


def test_coverage_counts_only_current_uncontradicted_verification():
    provider = _provider()
    request = RouteRequest("hola")
    coverage = model_coverage([provider], request)
    assert coverage["totals"]["cataloged_models"] == 4
    assert coverage["totals"]["eligible_models"] == 3
    assert coverage["totals"]["excluded_models"] == 1
    assert coverage["totals"]["verified_eligible_models"] == 1
    assert coverage["providers"][0]["verified_eligible_models"] == 1
    assert coverage["providers"][0]["excluded_by_reason"] == {
        "unsupported_capability": 1
    }
    assert len(rank([provider], request)) == 3


def test_admin_coverage_and_ranking_share_evidence_policy_without_network(monkeypatch):
    import router.api as api

    provider = _provider()

    class NoNetwork:
        def complete(self, *args, **kwargs):
            pytest.fail("unexpected inference")
        def discover_inventory(self, *args, **kwargs):
            pytest.fail("unexpected discovery")
        def probe_capability(self, *args, **kwargs):
            pytest.fail("unexpected probe")

    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "stage8-admin-only")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "stage8-app-only")
    monkeypatch.setattr(api, "router", SimpleNamespace(
        registry=SimpleNamespace(all=lambda: [provider]), client=NoNetwork()
    ))
    client = TestClient(api.app)
    coverage_path = "/diagnostics/model-coverage"
    ranking_path = "/diagnostics/model-ranking"
    payload = {"task": "hola", "required_capabilities": ["chat"]}

    assert client.get(coverage_path).status_code == 401
    assert client.get(coverage_path, headers={
        "Authorization": "Bearer stage8-app-only",
    }).status_code == 401
    assert client.post(ranking_path, json=payload).status_code == 401
    assert client.post(ranking_path, json=payload, headers={
        "Authorization": "Bearer stage8-app-only",
    }).status_code == 401

    headers = {"Authorization": "Bearer stage8-admin-only"}
    coverage_response = client.get(coverage_path, headers=headers)
    ranking_response = client.post(ranking_path, json=payload, headers=headers)
    assert coverage_response.status_code == ranking_response.status_code == 200
    coverage = coverage_response.json()
    ranking = ranking_response.json()
    assert coverage["totals"]["eligible_models"] == ranking["ranked_models"] == 3
    assert coverage["totals"]["verified_eligible_models"] == 1
    assert {row["model"]: row["required_capability_evidence"]["chat"]
            for row in ranking["ranking"]} == {
        "chat-legacy": "legacy_or_incomplete_evidence",
        "chat-fresh": "recorded_verified",
        "chat-inconclusive": "recorded_inconclusive",
    }
    assert ranking["inference_performed"] is False
    assert ranking["quality_measured"] is False
    assert "synthetic-key-never-used" not in ranking_response.text
    assert "stage8-admin-only" not in ranking_response.text

    invalid = client.post(ranking_path, json={
        "task": "hola", "required_capabilities": ["not-a-capability"],
    }, headers=headers)
    assert invalid.status_code == 422
