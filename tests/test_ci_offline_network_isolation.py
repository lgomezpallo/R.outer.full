"""Verify that CI's Python test suite cannot contact external inference APIs.

Only local sockets are permitted by the autouse fixture in conftest.py.
These assertions use numeric addresses; no DNS, credentials, or HTTP calls.
"""
import socket

import pytest

pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize("family,address", [
    (socket.AF_INET, ("1.1.1.1", 443)),
    (socket.AF_INET, ("203.0.113.5", 443)),
    (socket.AF_INET6, ("2001:db8::5", 443)),
])
@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_outbound_python_sockets_blocked_without_network(family, address, method):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match="offline_test_external_socket_blocked"):
            getattr(sock, method)(address)


def test_hostname_cannot_trigger_external_socket_or_dns_resolution():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match="offline_test_external_socket_blocked"):
            sock.connect(("api.groq.com", 443))
