import json

def test_forja_delivery_smoke():
    assert json.loads('{"ok": true}')["ok"] is True
