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


@pytest.fixture(autouse=True)
def prevent_external_sockets_in_pytest(monkeypatch):
    """Keep all Python tests offline, including accidental real provider calls.

    HTTP mocks still work; optional local loopback test servers still work.
    This covers Python socket connections in pytest, not subprocesses or
    account-side billing state. Production has no such test-only override.
    """
    import socket
    from ipaddress import ip_address

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_loopback_address(address):
        if not isinstance(address, tuple) or not address:
            return False
        host = address[0]
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except (ValueError, TypeError):
            return False

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback_address(address):
            raise RuntimeError("offline_test_external_socket_blocked")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback_address(address):
            raise RuntimeError("offline_test_external_socket_blocked")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
