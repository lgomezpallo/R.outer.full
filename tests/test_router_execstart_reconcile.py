"""P3: offline tests for env-wrapped ExecStart migration and owned rollback."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "router_execstart_reconcile.py"
spec = importlib.util.spec_from_file_location("router_execstart_reconcile", SOURCE)
assert spec and spec.loader
p3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p3)

SHA = "d" * 40
PIP_SCRIPT = (
    "import sys\n"
    "from uvicorn.main import main\n"
    "if __name__ == '__main__':\n"
    "    if sys.argv[0].endswith('.exe'):\n"
    "        sys.argv[0] = sys.argv[0][:-4]\n"
    "    sys.exit(main())\n"
)


def fixture(tmp_path):
    app = tmp_path / "checkout"
    release = tmp_path / "releases" / SHA
    for root in (app, release):
        (root / ".venv/bin").mkdir(parents=True)
        python = root / ".venv/bin/python"
        python.write_text("synthetic executable")
        python.chmod(0o755)
        uvicorn = root / ".venv/bin/uvicorn"
        uvicorn.write_text("#!" + str(python) + "\n" + PIP_SCRIPT)
        uvicorn.chmod(0o755)
    tokens = [
        "/usr/bin/env",
        "ROUTER_OFFLINE_ONLY=1",
        "ROUTER_DB_FILE=/private/router.db",
        "ROUTER_CATALOG_FILE=/private/router-catalog.json",
        str(release / ".venv/bin/uvicorn"),
        "router.api:app", "--host", "127.0.0.1",
        "--port", "8010", "--app-dir", str(release),
    ]
    assert len(tokens) == 12
    raw = "argv[]=" + " ".join(tokens) + " ; ignore_errors=no"
    kwargs = dict(app=app, releases=release.parent,
                  release_sha=SHA, verified_release_sha=SHA)
    return tokens, raw, kwargs


def test_exact_twelve_original_tokens_preserved_except_launcher(tmp_path):
    original, raw, kwargs = fixture(tmp_path)
    old, new = p3.canonicalize(raw, **kwargs)
    assert old == original
    assert new[:4] == old[:4]
    assert new[4:7] == [str(kwargs["app"] / ".venv/bin/python"), "-m", "uvicorn"]
    assert new[7:] == old[5:]
    assert new[new.index("--app-dir") + 1] == str(kwargs["releases"] / SHA)
    assert "ROUTER_OFFLINE_ONLY=1" in new
    assert p3.render_dropin(new).startswith(b"[Service]\nExecStart=\nExecStart=/usr/bin/env ")


def test_existing_canonical_inner_python_is_noop(tmp_path):
    original, raw, kwargs = fixture(tmp_path)
    original[4:5] = [str(kwargs["app"] / ".venv/bin/python"), "-m", "uvicorn"]
    # Canonical commands have 14 tokens; the observed 12-token layout is
    # deliberately pinned, so a changed live shape requires new review.
    with pytest.raises(p3.MigrationBlocked, match="execstart_shape_unverified"):
        p3.canonicalize("argv[]=" + " ".join(original) + " ; ignore_errors=no", **kwargs)


@pytest.mark.parametrize("change,reason", [
    (lambda a: a.__setitem__(0, "/bin/sh"), "env_wrapper_unverified"),
    (lambda a: a.__setitem__(1, "ROUTER_OFFLINE_ONLY=0"), "offline_or_data_environment_unverified"),
    (lambda a: a.__setitem__(2, "ROUTER_DB_FILE=relative.db"), "data_paths_not_absolute"),
    (lambda a: a.__setitem__(3, "GROQ_API_KEY=placeholder"), "env_assignment_unsupported"),
    (lambda a: a.__setitem__(5, "other.api:app"), "router_import_unverified"),
    (lambda a: a.__setitem__(7, "0.0.0.0"), "service_bind_unverified"),
    (lambda a: a.__setitem__(9, "8080"), "service_bind_unverified"),
    (lambda a: a.__setitem__(11, "/tmp/other"), "release_app_dir_unverified"),
])
def test_fail_closed_on_unverified_service_shapes(tmp_path, change, reason):
    original, _, kwargs = fixture(tmp_path)
    change(original)
    with pytest.raises(p3.MigrationBlocked, match=reason):
        p3.canonicalize("argv[]=" + " ".join(original) + " ; ignore_errors=no", **kwargs)


def test_rejects_unverified_release_and_launcher(tmp_path):
    original, raw, kwargs = fixture(tmp_path)
    with pytest.raises(p3.MigrationBlocked, match="release_sha_unverified"):
        p3.canonicalize(raw, **{**kwargs, "verified_release_sha": "a" * 40})
    launcher = Path(original[4])
    launcher.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(p3.MigrationBlocked, match="nested_launcher_unverified"):
        p3.canonicalize(raw, **kwargs)
    launcher.write_text("#!" + str(launcher.parent / "python") + "\n" + PIP_SCRIPT)
    launcher.chmod(0o644)
    with pytest.raises(p3.MigrationBlocked, match="nested_launcher_unverified"):
        p3.canonicalize(raw, **kwargs)
    launcher.chmod(0o755)
    launcher.unlink()
    launcher.symlink_to(launcher.parent / "python")
    with pytest.raises(p3.MigrationBlocked, match="nested_launcher_unverified"):
        p3.canonicalize(raw, **kwargs)


def test_canonical_python_rejects_symlink_to_untrusted_binary(tmp_path):
    original, raw, kwargs = fixture(tmp_path)
    canonical = kwargs["app"] / ".venv/bin/python"
    canonical.unlink()
    foreign = tmp_path / "untrusted-python3"
    foreign.write_text("synthetic")
    foreign.chmod(0o755)
    canonical.symlink_to(foreign)
    with pytest.raises(p3.MigrationBlocked, match="canonical_python_unverified_symlink"):
        p3.canonicalize(raw, **kwargs)


def test_rejects_unverified_argv_shape_and_symlink_release(tmp_path):
    original, raw, kwargs = fixture(tmp_path)
    with pytest.raises(p3.MigrationBlocked, match="execstart_shape_unverified"):
        p3.canonicalize(raw.replace(" --host ", " --reload --host "), **kwargs)
    with pytest.raises(p3.MigrationBlocked, match="execstart_unparseable"):
        p3.canonicalize("malformed", **kwargs)
    release = kwargs["releases"] / SHA
    moved = kwargs["releases"] / "real"
    release.rename(moved)
    release.symlink_to(moved, target_is_directory=True)
    with pytest.raises(p3.MigrationBlocked, match="release_directory_unsafe"):
        p3.canonicalize(raw, **kwargs)


def test_backup_private_atomic_install_and_rollback(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    unit = b"[Unit]\nDescription=Router\n[Service]\nExecStart=/original\n"
    original = b"[Service]\nExecStart=/original\n"
    p3.backup_original(unit, original, private)
    assert (private / "systemctl-cat.before").read_bytes() == unit
    assert (private / "release-dropin.before").read_bytes() == original
    assert (private / "systemctl-cat.before").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        p3.backup_original(unit, original, private)
    directory = tmp_path / "dropins"
    directory.mkdir()
    content = p3.render_dropin(["/usr/bin/env", "ROUTER_OFFLINE_ONLY=1", "/bin/true"])
    owned = p3.install_late_dropin(directory, content)
    path = directory / p3.LATE_DROPIN
    assert path.read_bytes() == content
    assert p3.identity(path) == owned
    with pytest.raises(p3.MigrationBlocked, match="dropin_already_exists"):
        p3.install_late_dropin(directory, b"foreign")
    assert path.read_bytes() == content
    assert p3.rollback_late_dropin(directory, owned)
    assert not path.exists()
    assert (private / "release-dropin.before").read_bytes() == original


def test_rollback_never_deletes_foreign_replacement_or_symlink(tmp_path):
    directory = tmp_path / "dropins"
    directory.mkdir()
    owned = p3.install_late_dropin(directory, b"[Service]\n")
    path = directory / p3.LATE_DROPIN
    path.unlink()
    path.write_bytes(b"foreign")
    assert not p3.rollback_late_dropin(directory, owned)
    assert path.read_bytes() == b"foreign"
    path.unlink()
    path.symlink_to(directory / "missing")
    assert not p3.rollback_late_dropin(directory, owned)
    assert path.is_symlink()


def test_atomic_install_rejects_existing_symlink_and_unsafe_backup(tmp_path):
    directory = tmp_path / "dropins"
    directory.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    path = directory / p3.LATE_DROPIN
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(p3.MigrationBlocked, match="dropin_already_exists"):
        p3.install_late_dropin(directory, b"content")
    assert path.is_symlink()
    assert list(target.iterdir()) == []
    private = tmp_path / "private"
    private.mkdir(mode=0o755)
    with pytest.raises(p3.MigrationBlocked, match="backup_directory_unsafe"):
        p3.backup_original(b"unit", b"dropin", private)


def test_rehearsal_is_offline_and_does_not_offer_production_apply(capsys):
    p3.rehearse()
    assert "isolated_execstart_rehearsal=passed" in capsys.readouterr().out
    source = SOURCE.read_text()
    assert "import subprocess" not in source
    assert "subprocess.run(" not in source
    assert "sudo" not in source.split('if __name__ == "__main__":')[1]
    assert "Only --rehearse is supported" in source
