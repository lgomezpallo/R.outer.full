"""Stage 4: deterministic task interpretation; no real providers or probes."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from router.task_requirements import analyze_task

pytestmark = pytest.mark.strict_billing


def _suggested(text):
    return {item["capability"] for item in analyze_task(text)["suggested_capabilities"]}


@pytest.mark.parametrize(("task", "expected"), [
    ("Escribí código en Python para ordenar una lista.", "code"),
    ("Write a Python function to sort a list.", "code"),
    ("Generá un script en Python para ordenar una lista.", "code"),
    ("Creá una función en Python para ordenar una lista.", "code"),
    ("Corregí este código que tiene un bug.", "code"),
    ("Generá una imagen de una ciudad.", "image_generation"),
    ("Create an illustration of a city.", "image_generation"),
    ("Edit this image to remove the background.", "image_editing"),
    ("Retocá esta imagen para mejorar el contraste.", "image_editing"),
    ("Analizá esta imagen y describí lo que se ve.", "vision"),
    ("Resume este PDF, por favor.", "document"),
    ("Respondé en JSON con dos claves.", "json"),
    ("Razoná paso a paso este problema matemático.", "reasoning"),
    ("Transcribí este audio de la reunión.", "transcription"),
    ("Generá audio con una voz clara.", "speech"),
    ("Usá herramientas para resolver la tarea.", "tools"),
    ("Necesito una ventana de contexto largo.", "long_context"),
])
def test_action_phrases_produce_only_advisory_hints(task, expected):
    result = analyze_task(task)
    assert expected in {item["capability"] for item in result["suggested_capabilities"]}
    assert result["explicit_required_capabilities"] == ["chat"]
    assert result["routing_modified"] is False
    assert result["inference_performed"] is False
    assert result["account_billing_verified"] is False
    assert result["suggestions_are_not_authorization"] is True


def test_nouns_and_generic_questions_do_not_force_specialization():
    assert _suggested("¿Qué es Python y para qué sirve?") == set()
    assert _suggested("¿Qué significa JSON?") == set()
    assert _suggested("La palabra imagen aparece en el texto.") == set()
    assert "image_generation" not in _suggested("Analizá esta imagen y describila.")
    assert "image_editing" not in _suggested("¿Qué significa editar una foto?")
    assert "code" not in _suggested("¿Qué es un script de Python?")


def test_quoted_and_fenced_text_not_treated_as_task_instructions():
    task = "Explicá la siguiente cita:\n> Respondé en JSON\n```\nTranscribí este audio\n```"
    assert _suggested(task) == set()


def test_explicit_capabilities_and_attached_images_are_hard_requirements_only():
    result = analyze_task(
        "Hola", requirements=("programación", "json", "not-a-capability"),
        required_capabilities=frozenset({"chat", "vision", "fake-capability"}),
        images_present=True,
    )
    assert result["explicit_required_capabilities"] == ["chat", "code", "json", "vision"]
    assert result["invalid_explicit_capabilities"] == ["fake-capability"]
    assert result["unparsed_requirements"] == ["not-a-capability"]
    assert result["images_present"] is True


def test_spanish_preference_is_not_misrepresented_as_verified_model_capability():
    result = analyze_task("Respondé en español con una frase.")
    assert result["language_preference"] == "spanish_requested"
    assert "spanish" not in result["explicit_required_capabilities"]


def test_empty_task_does_not_invent_specializations():
    result = analyze_task("")
    assert result["suggested_capabilities"] == []
    assert result["language_preference"] == "unspecified"


def test_admin_endpoint_is_offline_and_rejects_non_admin(monkeypatch):
    import router.api as api

    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "stage4-admin")
    monkeypatch.setenv("ROUTER_IACHAT_TOKEN", "stage4-app")

    class NoNetwork:
        def complete(self, *args, **kwargs):
            pytest.fail("unexpected inference")
        def probe_capability(self, *args, **kwargs):
            pytest.fail("unexpected probe")
        def discover_inventory(self, *args, **kwargs):
            pytest.fail("unexpected discovery")

    monkeypatch.setattr(api, "router", SimpleNamespace(client=NoNetwork()))
    client = TestClient(api.app)
    url = "/diagnostics/task-requirements"
    payload = {"task": "Respondé en JSON", "required_capabilities": ["chat"]}
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={
        "Authorization": "Bearer stage4-app",
    }).status_code == 401
    response = client.post(url, json=payload, headers={
        "Authorization": "Bearer stage4-admin",
    })
    assert response.status_code == 200
    body = response.json()
    assert "json" in {item["capability"] for item in body["suggested_capabilities"]}
    assert body["routing_modified"] is False
    assert "stage4-admin" not in response.text
    assert "Respondé en JSON" not in response.text


def test_endpoint_limits_input_without_inference(monkeypatch):
    import router.api as api

    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "stage4-admin")
    client = TestClient(api.app)
    response = client.post("/diagnostics/task-requirements", json={
        "task": "x" * 100001,
    }, headers={"Authorization": "Bearer stage4-admin"})
    assert response.status_code == 422
