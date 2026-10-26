"""Explicit test-only exception for algorithm fixtures using the non-routable .invalid TLD.

This exception is never present in application code. The real ProviderClient
independently checks canonical HTTPS endpoints before constructing HTTP.
"""
import pytest


@pytest.fixture
def allow_offline_selector_fixture(monkeypatch):
    from router import selector

    original = selector.billing_endpoint_ineligibility_reason

    def fixture_endpoint(provider_id, base_url, protocol):
        if base_url == "https://example.invalid/v1":
            return None
        return original(provider_id, base_url, protocol)

    monkeypatch.setattr(selector, "billing_endpoint_ineligibility_reason", fixture_endpoint)


@pytest.fixture(autouse=True)
def simulate_no_charge_account_for_legacy_offline_tests(request, monkeypatch):
    """Keep historical mock-based ranking tests independent of account billing.

    Only in pytest: pretend account authorization exists so algorithm tests
    remain meaningful. Strict billing regression tests opt out via marker and
    exercise the actual production default-deny behavior. There is no runtime
    configuration or production override for this fixture.
    """
    if request.node.get_closest_marker("strict_billing"):
        return
    from router import billing
    monkeypatch.setattr(billing, "account_billing_ineligibility_reason", lambda provider_id: None)
