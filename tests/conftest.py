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
