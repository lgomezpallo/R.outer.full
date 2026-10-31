"""No-network, no-credential checks of the pinned live-smoke release preflight."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "router_release_live_smoke.py"
spec = importlib.util.spec_from_file_location("live_smoke_preflight", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_missing_or_symlinked_frozen_release_fails_without_git(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "PINNED_SHA", "a" * 40)
    monkeypatch.setattr(smoke, "RELEASE", tmp_path / "absent")
    monkeypatch.setattr(smoke, "command", lambda *args: pytest.fail("git must not run"))
    with pytest.raises(smoke.SmokeBlocked, match="^frozen_release_missing$"):
        smoke.verify_frozen_release()

    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(smoke, "RELEASE", alias)
    with pytest.raises(smoke.SmokeBlocked, match="^frozen_release_missing$"):
        smoke.verify_frozen_release()


def test_pinned_release_matches_or_fails_categorically(tmp_path, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    monkeypatch.setattr(smoke, "RELEASE", release)
    monkeypatch.setattr(smoke, "PINNED_SHA", "a" * 40)
    monkeypatch.setattr(smoke, "command", lambda *args: "a" * 40)
    smoke.verify_frozen_release()

    monkeypatch.setattr(smoke, "command", lambda *args: "b" * 40)
    with pytest.raises(smoke.SmokeBlocked, match="^frozen_release_mismatch$"):
        smoke.verify_frozen_release()

    def broken_git(*args):
        raise subprocess.CalledProcessError(128, args)
    monkeypatch.setattr(smoke, "command", broken_git)
    with pytest.raises(smoke.SmokeBlocked, match="^frozen_release_invalid$"):
        smoke.verify_frozen_release()


def test_preflight_categories_are_fixed_and_before_any_provider_use():
    assert smoke.SAFE_PREFLIGHT_DIAGNOSTICS == frozenset({
        "frozen_release_missing", "frozen_release_invalid", "frozen_release_mismatch",
    })
    source = SCRIPT.read_text()
    assert source.count("    verify_frozen_release()") == 2
    assert source.index("    verify_frozen_release()", source.index("def internal(")) < source.index("    from router import api")
    assert 'print("live_smoke_preflight=" + str(error), flush=True)' in source
    assert "live_smoke_preflight=" not in source.split("def internal(", 1)[1].split("def orchestrate(", 1)[0]
