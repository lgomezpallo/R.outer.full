"""Sanitized Oracle pytest diagnostics: distinguish collection from assertion failures."""
from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from scripts.oracle_provisional_candidate import (
    TestOutcome,
    report_test_outcome,
    run_tests,
)


def _private_test(tmp_path: Path, source: str) -> tuple[Path, dict[str, str]]:
    candidate = tmp_path / "candidate"
    (candidate / "tests").mkdir(parents=True)
    (candidate / "tests" / "test_example.py").write_text(source)
    private = tmp_path / "private"
    private.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(private),
        "PYTHONPATH": str(candidate),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    return candidate, env


def test_private_diagnostic_distinguishes_missing_import_from_assertion(tmp_path: Path) -> None:
    candidate, env = _private_test(
        tmp_path,
        "import a_missing_oracle_dependency_private_name\n"
        "def test_unreachable():\n    assert True\n",
    )
    result = run_tests(Path(sys.executable), candidate, env)
    assert not result.ok
    assert result.phase == "collection_failed"
    assert result.errors >= 1
    assert result.missing_imports >= 1


def test_private_diagnostic_classifies_assertion_and_does_not_leak(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate, env = _private_test(
        tmp_path,
        "def test_private_oracle_name():\n"
        "    assert False, 'SECRET_NEVER_PRINT_IN_ACTIONS'\n",
    )
    result = run_tests(Path(sys.executable), candidate, env)
    assert not result.ok
    assert result.phase == "assertions_failed"
    assert result.failures == 1
    assert result.errors == 0
    report_test_outcome("baseline", result)
    public = capsys.readouterr()
    assert "router_provisional_baseline_tests=failed" in public.out
    assert "router_provisional_baseline_phase=assertions_failed" in public.out
    assert "router_provisional_baseline_failures=1" in public.out
    assert "SECRET_NEVER_PRINT_IN_ACTIONS" not in public.out + public.err
    assert "test_private_oracle_name" not in public.out + public.err
    assert "test_example.py" not in public.out + public.err


def test_private_diagnostic_passes_without_leaking_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate, env = _private_test(tmp_path, "def test_success():\n    assert True\n")
    result = run_tests(Path(sys.executable), candidate, env)
    assert result == TestOutcome(True, "passed")
    report_test_outcome("tracked", result)
    public = capsys.readouterr().out
    assert "router_provisional_tracked_tests=passed" in public
    assert "router_provisional_tracked_phase=passed" in public
    assert "test_success" not in public


def test_private_diagnostic_rejects_unknown_public_label(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ValueError, match="invalid diagnostic label"):
        report_test_outcome("secret_private_filename", TestOutcome(False, "other_failure"))
    assert capsys.readouterr().out == ""
