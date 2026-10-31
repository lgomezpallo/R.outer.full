"""Vision payload contract: enforce format and route only to visual models."""
import base64
from fastapi.testclient import TestClient
import router.api as module
from router.router import Router
from router.types import RouteRequest, RouteResponse
from router.client import ProviderClient


PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="
IMAGE = "data:image/png;base64," + PIXEL


def test_image_requires_vision_and_is_not_placed_in_text(monkeypatch):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "testtoken")
    module.router = Router()
    received = []

    def capture(request):
        received.append(request)
        return RouteResponse(ok=False, text=None, provider=None, model=None,
                             attempts=[], decisions=[], error="no_eligible_provider")

    monkeypatch.setattr(module.router, "process", capture)
    response = TestClient(module.app).post(
        "/route",
        headers={"Authorization": "Bearer testtoken"},
        json={"task": "Qué ves?", "images": [IMAGE]},
    )
    assert response.status_code == 200
    assert len(received) == 1
    request = received[0]
    assert request.images == (IMAGE,)
    assert {"chat", "vision"} <= request.required_capabilities
    assert request.decompose is False
    assert IMAGE not in request.task and IMAGE not in request.context
    messages = ProviderClient()._messages(request)
    assert messages[-1]["content"][1]["type"] == "image_url"
    assert messages[-1]["content"][1]["image_url"]["url"] == IMAGE


def test_reject_invalid_image_bytes_and_formats(monkeypatch):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "testtoken")
    client = TestClient(module.app)
    for value in (
        "https://somewhere.invalid/image.png",
        "data:image/svg+xml;base64," + PIXEL,
        "data:image/png;base64," + base64.b64encode(b"not a png").decode(),
        "data:image/png;base64,not%%%base64",
    ):
        response = client.post(
            "/route",
            headers={"Authorization": "Bearer testtoken"},
            json={"task": "Describe", "images": [value]},
        )
        assert response.status_code == 422


def test_iachat_five_image_contract_preserves_vision_and_bounds(monkeypatch):
    """Match IAchat's five-image limit without contacting a provider."""
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "synthetic-token")
    module.router = Router()
    received = []

    def capture(request):
        received.append(request)
        return RouteResponse(ok=False, text=None, provider=None, model=None,
                             attempts=[], decisions=[], error="no_eligible_provider")

    monkeypatch.setattr(module.router, "process", capture)
    client = TestClient(module.app)
    payload = {
        "task": "Compará estas cinco fotos",
        "images": [IMAGE] * 5,
        "required_capabilities": ["chat", "vision"],
        "decompose": False,
        "application_name": "iachat",
    }
    response = client.post("/route",
                           headers={"Authorization": "Bearer synthetic-token"},
                           json=payload)
    assert response.status_code == 200
    assert response.json()["error"] == "no_eligible_provider"
    assert len(received) == 1
    assert received[0].images == (IMAGE,) * 5
    assert {"chat", "vision"} <= received[0].required_capabilities
    assert received[0].decompose is False
    assert all(IMAGE not in field for field in (received[0].task, received[0].context))

    payload["images"] = [IMAGE] * 6
    rejected = client.post("/route",
                           headers={"Authorization": "Bearer synthetic-token"},
                           json=payload)
    assert rejected.status_code == 422
    assert len(received) == 1
