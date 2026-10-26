"""Inventory provenance is offline metadata, not live API or billing evidence."""
from datetime import datetime
import json

import pytest

from router.catalog import ModelSpec, ProviderCatalog, ProviderSpec
from router.inventory import inventory_report
from router.registry import RegisteredProvider
from router.router import Router
from router.state import RuntimeState

pytestmark = pytest.mark.strict_billing

GROQ_URL = "https://api.groq.com/openai/v1"


def _catalog(path=None):
    spec = ProviderSpec(
        "groq", ("groq",), GROQ_URL,
        (ModelSpec("old", frozenset({"chat"}), "standard", billing_status="free"),),
    )
    return ProviderCatalog(providers=(spec,), storage_path=path)


def test_successful_local_merge_has_utc_timestamp_and_survives_restart(tmp_path):
    path = tmp_path / "inventory.json"
    catalog = _catalog(path)
    updated = catalog.merge_discovered_models("groq", ["old"], {"old": "free"})
    assert updated.last_inventory_source == "catalog_merge"
    parsed = datetime.fromisoformat(updated.last_inventory_merge_at)
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0

    restored = ProviderCatalog(providers=(), storage_path=path).get("groq")
    assert restored.last_inventory_merge_at == updated.last_inventory_merge_at
    assert restored.last_inventory_source == "catalog_merge"

    registered = RegisteredProvider(restored, "offline-placeholder", RuntimeState())
    report = inventory_report([registered])
    row = report["providers"][0]
    assert row["last_inventory_merge_at"] == updated.last_inventory_merge_at
    assert row["last_inventory_source"] == "catalog_merge"
    assert report["merge_timestamps_do_not_prove_live_discovery"] is True
    assert report["live_discovery_performed"] is False
    assert report["account_billing_verified"] is False
    assert row["counts"]["eligible"] == 0


def test_discovery_client_path_records_source_without_live_claim():
    class OfflineInventory:
        def discover_inventory(self, provider):
            return ["old", "new"], {"old": "unknown", "new": "free"}

    router = Router(client=OfflineInventory(), catalog=_catalog())
    provider = router.add_provider("groq", "offline-placeholder", discover=True)
    assert provider.spec.last_inventory_source == "client_discover_inventory"
    assert provider.spec.last_inventory_merge_at is not None
    assert {m.id: m.billing_status for m in provider.spec.models} == {
        "old": "unknown", "new": "free"
    }
    report = inventory_report([provider])
    assert report["totals"]["eligible"] == 0
    assert report["discovery_freshness"] == "not_verified"


def test_failed_discovery_preserves_previous_provenance_and_price():
    catalog = _catalog()
    original = catalog.merge_discovered_models("groq", ["old"], {"old": "unknown"})

    class OfflineFailure:
        def discover_inventory(self, provider):
            raise RuntimeError("offline_discovery_failure")

    router = Router(client=OfflineFailure(), catalog=catalog)
    provider = router.add_provider("groq", "offline-placeholder", discover=True)
    assert provider.spec.last_inventory_merge_at == original.last_inventory_merge_at
    assert provider.spec.last_inventory_source == "catalog_merge"
    assert provider.spec.models[0].billing_status == "unknown"


def test_empty_successful_snapshot_updates_provenance_and_revokes_free():
    catalog = _catalog()
    catalog.merge_discovered_models("groq", ["old"], {"old": "free"})

    class OfflineEmpty:
        def discover_inventory(self, provider):
            return [], {}

    provider = Router(client=OfflineEmpty(), catalog=catalog).add_provider(
        "groq", "offline-placeholder", discover=True
    )
    assert provider.spec.last_inventory_source == "client_discover_inventory"
    assert provider.spec.last_inventory_merge_at is not None
    assert provider.spec.models[0].billing_status == "unknown"
    assert provider.spec.models[0].discovered_in_last_snapshot is False


def test_legacy_catalog_has_unknown_provenance_not_fabricated(tmp_path):
    path = tmp_path / "old.json"
    catalog = _catalog(path)
    catalog.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["providers"][0].pop("last_inventory_merge_at", None)
    data["providers"][0].pop("last_inventory_source", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    restored = ProviderCatalog(providers=(), storage_path=path).get("groq")
    assert restored.last_inventory_merge_at is None
    assert restored.last_inventory_source is None


def test_invalid_source_is_rejected_without_changing_catalog():
    catalog = _catalog()
    with pytest.raises(ValueError, match="invalid_inventory_source"):
        catalog.merge_discovered_models(
            "groq", ["old"], {"old": "free"}, source="verified_live"
        )
    assert catalog.get("groq").last_inventory_merge_at is None
