from router.state import RuntimeState
from router.storage import RouterStore

def test_encrypted_credential_roundtrip(tmp_path):
    store = RouterStore(tmp_path / "router.db", master_key="11" * 32)
    store.save_credential("groq", "secret-value")
    loaded = store.load_credentials()
    assert [(item.provider_id, item.api_key) for item in loaded] == [("groq", "secret-value")]

def test_provider_runtime_survives_restart(tmp_path):
    path = tmp_path / "router.db"
    store = RouterStore(path)
    state = RuntimeState()
    state.mark_failure("model-a", "rate limit", 60)
    store.save_provider_runtime("provider-a", state)

    loaded = RouterStore(path).load_provider_runtime("provider-a")
    assert loaded is not None
    assert loaded["failure_count"] == 1
    assert loaded["health_ok"] is False
    assert loaded["cooldown_until"] > 0

def test_app_tokens_are_hashed_and_verifiable(tmp_path):
    store = RouterStore(tmp_path / "router.db")
    token = store.create_app_token("iachat")
    assert token.startswith("rtr_")
    assert store.verify_app_token(token) == "iachat"
    listed = store.list_app_tokens()
    assert listed[0]["name"] == "iachat"
    assert token not in str(listed)
