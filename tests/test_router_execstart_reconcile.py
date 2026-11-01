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



def test_backup_directory_swap_cannot_redirect_private_contents(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    moved = tmp_path / "private-original"
    original_open = os.open
    swapped = False

    def swap_after_directory_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "systemctl-cat.before" and not swapped:
            swapped = True
            private.rename(moved)
            private.symlink_to(public, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(p3.os, "open", swap_after_directory_open)
    p3.backup_original(b"private unit", b"private dropin", private)
    assert swapped
    assert (moved / "systemctl-cat.before").read_bytes() == b"private unit"
    assert (moved / "release-dropin.before").read_bytes() == b"private dropin"
    assert list(public.iterdir()) == []


def test_backup_rejects_initial_symlink_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(p3.MigrationBlocked, match="backup_directory_unsafe"):
        p3.backup_original(b"unit", b"dropin", alias)
    assert list(real.iterdir()) == []


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


def test_dropin_precedence_requires_same_effective_four_and_late_sorting():
    paths = [
        "/etc/systemd/system/router.service.d/10-runtime.conf",
        "/etc/systemd/system/router.service.d/30-offline.conf",
        "/etc/systemd/system/router.service.d/50-release.conf",
        "/etc/systemd/system/router.service.d/90-guard.conf",
    ]
    p3.verify_dropin_precedence(paths)
    with pytest.raises(p3.MigrationBlocked, match="dropin_inventory_changed"):
        p3.verify_dropin_precedence(paths[:-1])
    with pytest.raises(p3.MigrationBlocked, match="dropin_precedence_unverified"):
        p3.verify_dropin_precedence(paths[:-1] + [
            "/etc/systemd/system/router.service.d/zzzzzz-override.conf"])
    with pytest.raises(p3.MigrationBlocked, match="dropin_precedence_unverified"):
        p3.verify_dropin_precedence(paths[:-1] + [
            "/etc/systemd/system/router.service.d/" + p3.LATE_DROPIN])
    with pytest.raises(p3.MigrationBlocked, match="dropin_inventory_unsafe"):
        p3.verify_dropin_precedence(paths[:-1] + [paths[0]])
    with pytest.raises(p3.MigrationBlocked, match="dropin_inventory_unsafe"):
        p3.verify_dropin_precedence(paths[:-1] + ["relative.conf"])
    with pytest.raises(p3.MigrationBlocked, match="dropin_inventory_unsafe"):
        p3.verify_dropin_precedence(paths[:-1] + ["/tmp/other.service.d/80-late.conf"])
    with pytest.raises(p3.MigrationBlocked, match="dropin_inventory_unsafe"):
        p3.verify_dropin_precedence(paths[:-1] + ["/etc/systemd/system/../router.service.d/80-late.conf"])


def test_rollback_race_quarantines_then_restores_foreign_file(tmp_path, monkeypatch):
    directory = tmp_path / "dropins"
    directory.mkdir()
    owned = p3.install_late_dropin(directory, b"owned")
    path = directory / p3.LATE_DROPIN
    original_rename = os.rename
    swapped = False

    def replace_at_move(src, dst, *args, **kwargs):
        nonlocal swapped
        if src == p3.LATE_DROPIN and dst == "held" and not swapped:
            swapped = True
            path.unlink()
            path.write_bytes(b"foreign-after-precheck")
        return original_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(p3.os, "rename", replace_at_move)
    assert not p3.rollback_late_dropin(directory, owned)
    assert swapped
    assert path.read_bytes() == b"foreign-after-precheck"
    assert not list(directory.glob(".router-rollback-*"))


def test_rollback_race_preserves_foreign_if_name_reoccupied(tmp_path, monkeypatch):
    directory = tmp_path / "dropins"
    directory.mkdir()
    owned = p3.install_late_dropin(directory, b"owned")
    path = directory / p3.LATE_DROPIN
    original_rename = os.rename
    original_link = os.link

    def replace_at_move(src, dst, *args, **kwargs):
        if src == p3.LATE_DROPIN and dst == "held":
            path.unlink()
            path.write_bytes(b"first-foreign")
        return original_rename(src, dst, *args, **kwargs)

    def reoccupy_before_restore(src, dst, *args, **kwargs):
        if src == "held" and dst == p3.LATE_DROPIN:
            path.write_bytes(b"second-foreign")
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(p3.os, "rename", replace_at_move)
    monkeypatch.setattr(p3.os, "link", reoccupy_before_restore)
    assert not p3.rollback_late_dropin(directory, owned)
    assert path.read_bytes() == b"second-foreign"
    quarantines = list(directory.glob(".router-rollback-*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "held").read_bytes() == b"first-foreign"


def test_rollback_race_preserves_foreign_symlink(tmp_path, monkeypatch):
    directory = tmp_path / "dropins"
    directory.mkdir()
    owned = p3.install_late_dropin(directory, b"owned")
    path = directory / p3.LATE_DROPIN
    target = tmp_path / "foreign-target"
    target.write_bytes(b"untouched")
    original_rename = os.rename

    def replace_at_move(src, dst, *args, **kwargs):
        if src == p3.LATE_DROPIN and dst == "held":
            path.unlink()
            path.symlink_to(target)
        return original_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(p3.os, "rename", replace_at_move)
    assert not p3.rollback_late_dropin(directory, owned)
    assert path.is_symlink()
    assert path.resolve() == target
    assert target.read_bytes() == b"untouched"


def test_shebang_probe_classifies_expected_release_and_checkout_interpreters(tmp_path):
    original, _, kwargs = fixture(tmp_path)
    checkout = kwargs["app"]
    release = kwargs["releases"] / SHA
    probe = lambda launcher: p3.classify_nested_shebang(
        launcher, checkout=checkout, release=release)
    assert probe(Path(original[4])) == "expected_release_venv_python_executable"
    launcher = checkout / ".venv/bin/uvicorn"
    alt = checkout / ".venv/bin/python3"
    alt.write_text("synthetic python")
    alt.chmod(0o755)
    launcher.write_text("#!" + str(alt) + "\n" + PIP_SCRIPT)
    assert probe(launcher) == "expected_checkout_venv_python_executable"


def test_shebang_probe_distinguishes_unsupported_and_missing_interpreters(tmp_path):
    original, _, kwargs = fixture(tmp_path)
    checkout = kwargs["app"]
    release = kwargs["releases"] / SHA
    launcher = Path(original[4])
    probe = lambda: p3.classify_nested_shebang(
        launcher, checkout=checkout, release=release)
    launcher.write_text("#!/usr/bin/env python3\n" + PIP_SCRIPT)
    assert probe() == "shebang_arguments_unsupported"
    launcher.write_text("#!/usr/bin/python3\n" + PIP_SCRIPT)
    assert probe() == "interpreter_outside_expected_venv"
    launcher.write_text("#!" + str(release / ".venv/bin/python3") + "\n" + PIP_SCRIPT)
    assert probe() == "interpreter_missing"
    interpreter = release / ".venv/bin/python3"
    interpreter.write_text("not executable")
    interpreter.chmod(0o644)
    assert probe() == "interpreter_not_executable"
    launcher.write_text("not a script\n")
    assert probe() == "shebang_missing_or_long"


def test_shebang_probe_rejects_untrusted_symlink_and_never_leaks_paths(tmp_path):
    original, _, kwargs = fixture(tmp_path)
    checkout = kwargs["app"]
    release = kwargs["releases"] / SHA
    launcher = Path(original[4])
    interpreter = release / ".venv/bin/python3"
    foreign = tmp_path / "private-sensitive-path" / "python3"
    foreign.parent.mkdir()
    foreign.write_text("synthetic")
    foreign.chmod(0o755)
    interpreter.symlink_to(foreign)
    launcher.write_text("#!" + str(interpreter) + "\n" + PIP_SCRIPT)
    label = p3.classify_nested_shebang(launcher, checkout=checkout, release=release)
    assert label == "interpreter_symlink_untrusted"
    assert str(tmp_path) not in label
    assert "private-sensitive-path" not in label
    assert p3.classify_nested_shebang(foreign, checkout=checkout, release=release) == "launcher_outside_expected"

def test_oracle_observed_checkout_python3_system_symlink_is_accepted(tmp_path):
    # SSH read-only #41329983185: absolute checkout venv Python alias,
    # executable symlink to system Python, recognized pip Uvicorn body.
    system = Path("/usr/bin/python3")
    if not system.is_file() or not os.access(system, os.X_OK):
        pytest.skip("system Python alias unavailable on this runner")
    original, _, kwargs = fixture(tmp_path)
    checkout = kwargs["app"]
    launcher = checkout / ".venv/bin/uvicorn"
    alias = checkout / ".venv/bin/python3"
    alias.symlink_to(system)
    launcher.write_text("#!" + str(alias) + "\n" + PIP_SCRIPT)
    original[4] = str(launcher)
    raw = "argv[]=" + " ".join(original) + " ; ignore_errors=no"
    old, new = p3.canonicalize(raw, **kwargs)
    assert old == original
    assert new[4:7] == [str(checkout / ".venv/bin/python"), "-m", "uvicorn"]
    assert new[:4] == old[:4]
    assert new[7:] == old[5:]


def test_alternate_shebang_rejects_foreign_symlink_and_fake_python_binary(tmp_path):
    original, _, kwargs = fixture(tmp_path)
    checkout = kwargs["app"]
    launcher = checkout / ".venv/bin/uvicorn"
    alias = checkout / ".venv/bin/python3"
    foreign = tmp_path / "foreign" / "python3"
    foreign.parent.mkdir()
    foreign.write_text("untrusted executable")
    foreign.chmod(0o755)
    alias.symlink_to(foreign)
    launcher.write_text("#!" + str(alias) + "\n" + PIP_SCRIPT)
    original[4] = str(launcher)
    raw = "argv[]=" + " ".join(original) + " ; ignore_errors=no"
    with pytest.raises(p3.MigrationBlocked, match="nested_launcher_unverified"):
        p3.canonicalize(raw, **kwargs)
    alias.unlink()
    alias.write_text("untrusted executable")
    alias.chmod(0o755)
    with pytest.raises(p3.MigrationBlocked, match="nested_launcher_unverified"):
        p3.canonicalize(raw, **kwargs)


def test_full_effective_snapshot_preserves_four_dropins_and_offline_exec(tmp_path):
    private = tmp_path / "snapshot"
    private.mkdir(mode=0o700)
    dropins = {
        "10-runtime.conf": b"[Service]\\nEnvironment=ROUTER_OFFLINE_ONLY=1\\n",
        "30-offline.conf": b"[Service]\\n",
        "50-release.conf": b"[Service]\\nExecStart=/usr/bin/env ROUTER_OFFLINE_ONLY=1\\n",
        "90-guard.conf": b"[Service]\\n",
    }
    cat = b"# unit and all drop-ins, byte-for-byte\\n" + b"".join(dropins.values())
    effective = b"argv[]=/usr/bin/env ROUTER_OFFLINE_ONLY=1 ; ignore_errors=no"
    p3.backup_effective_service_snapshot(
        unit_cat=cat, dropins=dropins, effective_exec=effective,
        state="active", private=private,
    )
    assert (private / "systemctl-cat.before").read_bytes() == cat
    assert (private / "effective-execstart.before").read_bytes() == effective
    assert (private / "service-state.before").read_bytes() == b"active\\n"
    names = sorted(dropins)
    assert (private / "dropin-names.before").read_text().splitlines() == names
    for i, name in enumerate(names):
        item = private / f"dropin-{i:02d}.before"
        assert item.read_bytes() == dropins[name]
        assert item.stat().st_mode & 0o777 == 0o600
    with pytest.raises(p3.MigrationBlocked, match="service_snapshot_directory_unsafe"):
        p3.backup_effective_service_snapshot(
            unit_cat=cat, dropins=dropins, effective_exec=effective,
            state="active", private=private,
        )


def test_full_snapshot_fails_closed_before_writing(tmp_path):
    private = tmp_path / "snapshot"
    private.mkdir(mode=0o700)
    drops = {f"{i}0-entry.conf": b"[Service]\\n" for i in range(4)}
    args = dict(unit_cat=b"unit", dropins=drops,
                effective_exec=b"ROUTER_OFFLINE_ONLY=1", state="active", private=private)
    for bad in (
        {**args, "state": "inactive"},
        {**args, "effective_exec": b"no-offline-flag"},
        {**args, "dropins": {**drops, "zzzzzz-foreign.conf": b"foreign"}},
    ):
        with pytest.raises(p3.MigrationBlocked):
            p3.backup_effective_service_snapshot(**bad)
        assert list(private.iterdir()) == []
    private.chmod(0o755)
    with pytest.raises(p3.MigrationBlocked, match="service_snapshot_directory_unsafe"):
        p3.backup_effective_service_snapshot(**args)
