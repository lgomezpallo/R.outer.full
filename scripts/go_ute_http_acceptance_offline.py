"""Real G.O. Router client -> Router HTTP, in one network-isolated container.

This test uses the unmodified go_ute/router.py from G.O. main at
5823272f3cd2c0c6111282a29dd2be0482562b9d, as a pinned test fixture.
It starts the actual Router ASGI server on loopback, sends no provider traffic,
and checks G.O.'s production client contract, not a hand-written substitute.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

HOST = "http://127.0.0.1:8010"
TOKEN = "ci-offline-only-token"


def main() -> None:
    assert os.environ["ROUTER_OFFLINE_ONLY"] == "1"
    assert os.environ["ROUTER_SERVICE_TOKEN"] == TOKEN
    assert not any(os.environ.get(name) for name in (
        "GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY",
        "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_KEY",
    )), "provider credentials must not enter the offline acceptance container"
    spec = importlib.util.spec_from_file_location("go_ute_client_snapshot", "/tmp/go_ute_router.py")
    assert spec and spec.loader
    go = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(go)

    os.environ["GO_ROUTER_URL"] = HOST
    os.environ["GO_ROUTER_TOKEN"] = TOKEN
    os.environ["GO_ROUTER_ENABLED"] = "0"
    try:
        go.answer([{"role": "user", "content": "Estado de las obras"}])
    except go.RouterUnavailable as exc:
        assert str(exc) == "router_pending_validation", str(exc)
    else:
        raise AssertionError("G.O. bypassed its disabled-by-default gate")

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "router.api:app",
         "--host", "127.0.0.1", "--port", "8010"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            for _ in range(80):
                assert server.poll() is None, "Router server exited before HTTP readiness"
                try:
                    ready = client.get(HOST + "/health")
                    if ready.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Router did not become ready on isolated loopback")

            # The actual G.O. probe sends only invalid requests; it must
            # authenticate correctly without reaching an inference handler.
            contract = go.probe_contract()
            assert contract == {
                "status": "verified", "anonymous_http": 401, "authorized_http": 422,
            }, contract

            request = {
                "task": "Estado de obras UTE",
                "conversation": [{"role": "user", "content": "Buen día"}],
                "context": "Responder sin inventar datos",
                "required_capabilities": ["chat"],
                "application_name": "go-ute",
                "decompose": False,
            }
            assert client.post(HOST + "/route", json=request).status_code == 401
            response = client.post(
                HOST + "/route", json=request,
                headers={"Authorization": "Bearer " + TOKEN},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is False and payload["text"] is None, payload
            assert payload["error"] == "no_eligible_provider", payload
            assert payload["attempts"] == [], payload

            # G.O.'s actual answer() must reject the offline failure, not
            # present it as a fabricated successful conversation.
            os.environ["GO_ROUTER_ENABLED"] = "1"
            try:
                go.answer([
                    {"role": "user", "content": "Buen día"},
                    {"role": "user", "content": "Estado de obras UTE"},
                ])
            except go.RouterUnavailable as exc:
                assert str(exc) == "router_invalid_response", str(exc)
            else:
                raise AssertionError("G.O. accepted a no-provider response")

            completion = client.post(
                HOST + "/v1/chat/completions",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"messages": [{"role": "user", "content": "hola"}], "stream": False},
            )
            assert completion.status_code == 502, completion.text
            assert "no_eligible_provider" in completion.text
            print("go_ute_real_client_loopback_offline_acceptance=passed")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
