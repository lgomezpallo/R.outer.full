from __future__ import annotations
from ipaddress import ip_address
from urllib.parse import urlparse

def validate_provider_base_url(value: str) -> str:
    raw = value.strip()
    if len(raw) > 2048:
        raise ValueError("invalid_provider_base_url")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_provider_base_url")
    host = parsed.hostname.lower().strip("[]")
    if host in {"localhost"} or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("private_provider_base_url")
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError("private_provider_base_url")
    path = parsed.path.rstrip("/")
    if path.lower().endswith(("/models", "/chat/completions")):
        raise ValueError("provider_base_url_must_not_include_endpoint")
    return f"{parsed.scheme}://{parsed.netloc}{path}"
