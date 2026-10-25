"""Commit status reporting is intentionally limited to non-sensitive metadata."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/report_oracle_status.py"
spec = importlib.util.spec_from_file_location("report_oracle_status", SCRIPT)
assert spec is not None and spec.loader is not None
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


@pytest.mark.parametrize(("outcome", "state"), [
    ("success", "success"),
    ("failure", "failure"),
    ("skipped", "error"),
    ("cancelled", "error"),
    ("unknown", "error"),
])
def test_status_payload_contains_only_fixed_safe_fields(outcome: str, state: str):
    result = reporter.build_payload(outcome, "lgomezpallo/R.outer.full", "12345")
    assert result["state"] == state
    assert result["context"] == "router/oracle-reconcile"
    assert result["target_url"] == "https://github.com/lgomezpallo/R.outer.full/actions/runs/12345"
    assert set(result) == {"state", "context", "description", "target_url"}
    assert len(result["description"]) <= 140


@pytest.mark.parametrize(("repo", "run"), [
    ("bad repo", "1"), ("owner/repo", "not-an-id"), ("owner/repo/extra", "1"),
])
def test_invalid_status_coordinates_are_rejected(repo: str, run: str):
    with pytest.raises(ValueError):
        reporter.build_payload("success", repo, run)


def test_publish_never_puts_token_in_payload_or_stdout(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "lgomezpallo/R.outer.full")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_TOKEN", "SECRET_TOKEN_MUST_NOT_LEAK")
    monkeypatch.setenv("RECONCILE_OUTCOME", "failure")
    captured = {}

    class Response:
        status = 201
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode())
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(reporter.urllib.request, "urlopen", fake_open)
    reporter.publish()
    assert captured["url"].endswith("/statuses/" + "a" * 40)
    assert captured["payload"]["state"] == "failure"
    assert captured["authorization"] == "Bearer SECRET_TOKEN_MUST_NOT_LEAK"
    assert "SECRET_TOKEN_MUST_NOT_LEAK" not in json.dumps(captured["payload"])
    assert "SECRET_TOKEN_MUST_NOT_LEAK" not in capsys.readouterr().out


def test_workflow_reports_even_when_oracle_check_fails():
    workflow = (Path(__file__).resolve().parents[1] /
                ".github/workflows/router-oracle-monitor.yml").read_text()
    assert "statuses: write" in workflow
    assert "id: reconcile" in workflow
    assert "if: always()" in workflow
    assert "run: python3 scripts/report_oracle_status.py" in workflow
