"""Safe, non-deploying Oracle-only collision behavior probe."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/oracle_collision_probe.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fixtures(tmp_path: Path) -> tuple[Path, Path]:
    app, candidate = tmp_path / "oracle", tmp_path / "candidate"
    for repo in (app, candidate):
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "Router Tests")
        git(repo, "config", "user.email", "test@example.invalid")
        (repo / "router").mkdir()
        (repo / "router/__init__.py").write_text("")
        (repo / "tests").mkdir()
        (repo / "README.md").write_text("base\n")
    (candidate / "router/collision.py").write_text("VALUE = 1\n")
    (candidate / "tests/test_collision.py").write_text(
        "from router.collision import VALUE\n"
        "def test_collision():\n    assert VALUE == 2\n")
    for repo in (app, candidate):
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
    (app / "router/collision.py").write_text("VALUE = 2\n")
    return app, candidate


def run(app: Path, candidate: Path) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONPATH": str(candidate), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(candidate.parent)}
    return subprocess.run([sys.executable, str(SCRIPT), str(app), str(candidate), sys.executable],
                          capture_output=True, text=True, env=env, check=False)


def test_collision_alternate_passes_and_candidate_restored(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    before = (candidate / "router/collision.py").read_bytes()
    result = run(app, candidate)
    assert result.returncode == 0, result.stderr
    assert "oracle_collision_status=tested_private_variant" in result.stdout
    assert "oracle_collision_tests=passed" in result.stdout
    assert (candidate / "router/collision.py").read_bytes() == before
    assert (app / "router/collision.py").read_text() == "VALUE = 2\n"
    assert "router/collision.py" not in result.stdout + result.stderr


def test_collision_alternate_failure_is_private_and_restored(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    (app / "router/collision.py").write_text("VALUE = 3\nPRIVATE_SECRET = 'NO_LOG'\n")
    before = (candidate / "router/collision.py").read_bytes()
    result = run(app, candidate)
    assert result.returncode == 0
    assert "oracle_collision_tests=failed" in result.stdout
    assert "NO_LOG" not in result.stdout + result.stderr
    assert (candidate / "router/collision.py").read_bytes() == before


def test_identical_collision_needs_no_execution(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    (app / "router/collision.py").write_text("VALUE = 1\n")
    result = run(app, candidate)
    assert "oracle_collision_status=identical" in result.stdout
    assert "oracle_collision_tests=skipped" in result.stdout


def test_symlink_and_multiple_collisions_do_not_change_candidate(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    before = (candidate / "router/collision.py").read_bytes()
    (app / "router/collision.py").unlink()
    (app / "router/collision.py").symlink_to("/etc/passwd")
    result = run(app, candidate)
    assert "oracle_collision_status=blocked_path" in result.stdout
    assert (candidate / "router/collision.py").read_bytes() == before
    (app / "router/collision.py").unlink()
    (app / "router/collision.py").write_text("VALUE = 2\n")
    (app / "router/other.py").write_text("VALUE = 4\n")
    (candidate / "router/other.py").write_text("VALUE = 5\n")
    git(candidate, "add", ".")
    git(candidate, "commit", "-qm", "add another tracked target")
    result = run(app, candidate)
    assert "oracle_collision_status=multiple" in result.stdout
    assert (candidate / "router/collision.py").read_bytes() == before


def test_rejects_candidate_nested_under_live_app(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    result = run(app, app / "router")
    assert "oracle_collision_status=blocked_layout" in result.stdout


def test_staged_untracked_overlay_does_not_count_as_collision(tmp_path: Path):
    app, candidate = fixtures(tmp_path)
    (app / "router/overlay.py").write_text("VALUE = 3\n")
    (candidate / "router/overlay.py").write_text("VALUE = 3\n")
    before = (candidate / "router/collision.py").read_bytes()
    result = run(app, candidate)
    assert result.returncode == 0
    assert "oracle_collision_status=tested_private_variant" in result.stdout
    assert "oracle_collision_tests=passed" in result.stdout
    assert (candidate / "router/collision.py").read_bytes() == before
