from router.client import ProviderClient
from router.types import RouteRequest

def test_structured_history_preserves_roles_and_order():
    req = RouteRequest(
        task="¿Y cuándo?",
        context="Contestá en español.",
        conversation=(("user", "¿Cuándo juega River?"), ("assistant", "El domingo.")),
    )
    messages = ProviderClient()._messages(req)
    assert messages == [
        {"role": "system", "content": "Contestá en español."},
        {"role": "user", "content": "¿Cuándo juega River?"},
        {"role": "assistant", "content": "El domingo."},
        {"role": "user", "content": "¿Y cuándo?"},
    ]


def test_legacy_context_still_works_without_conversation():
    req = RouteRequest(task="Hola", context="Responde en español.")
    assert ProviderClient()._messages(req) == [
        {"role": "system", "content": "Responde en español."},
        {"role": "user", "content": "Hola"},
    ]
