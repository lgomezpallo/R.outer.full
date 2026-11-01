"""Isolated P3 one-shot adapter tests. No systemctl or Oracle access."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import router_execstart_reconcile as p3
import router_p3_one_shot as one

SHA = "d" * 40
PIP = (
    "import sys\n"
    "from uvicorn.main import main\n"
    "if __name__ == '__main__':\n"
    "    if sys.argv[0].endswith('.exe'):\n"
    "        sys.argv[0] = sys.argv[0][:-4]\n"
    "    sys.exit(main())\n"
)


class FakeSystem:
    def __init__(self, directory: Path, raw: bytes):
        self.directory = directory
        self.original = raw
        self.candidate = None
        self.restarts = 0
        self.reloads = 0
        self.fail_first = False
        self.interrupt_first = False
        self.replace_first = False
        self.health_ok = True
        self.switched = False

    def capture(self):
        names = sorted(p.name for p in self.directory.iterdir() if p.suffix == ".conf")
        drops = {n: (self.directory / n).read_bytes() for n in names}
        paths = tuple(str(self.directory / n) for n in names)
        unit = b"unit\n" + b"".join(
            n.encode() + b"\n" + drops[n] for n in names)
        raw = self.original
        if self.switched and self.candidate is not None:
            raw = ("argv[]=" + " ".join(self.candidate) +
                   " ; ignore_errors=no").encode()
        return one.Observed(unit, raw, paths, drops, "active", self.health_ok)

    def reload(self):
        self.reloads += 1

    def restart(self):
        self.restarts += 1
        self.switched = (self.directory / p3.LATE_DROPIN).exists()
        if self.restarts == 1 and self.replace_first:
            (self.directory / p3.LATE_DROPIN).write_bytes(b"foreign")
        if self.restarts == 1 and self.interrupt_first:
            raise one.Interrupted("operator_signal_received")
        if self.restarts == 1 and self.fail_first:
            raise one.OperationalBlock("system_command_failed")


def fixture(tmp_path, monkeypatch):
    app = tmp_path / "checkout"
    release = tmp_path / "releases" / SHA
    for root in (app, release):
        bin_dir = root / ".venv/bin"
        bin_dir.mkdir(parents=True)
        python = bin_dir / "python"
        python.write_text("synthetic")
        python.chmod(0o755)
    uvicorn = release / ".venv/bin/uvicorn"
    uvicorn.write_text("#!" + str(release / ".venv/bin/python") + "\n" + PIP)
    uvicorn.chmod(0o755)
    argv = [
        "/usr/bin/env", "ROUTER_OFFLINE_ONLY=1",
        "ROUTER_DB_FILE=/private/router.db",
        "ROUTER_CATALOG_FILE=/private/catalog.json",
        str(uvicorn), "router.api:app", "--host", "127.0.0.1",
        "--port", "8010", "--app-dir", str(release),
    ]
    directory = tmp_path / "router.service.d"
    directory.mkdir()
    for name in ("10-runtime.conf", "30-offline.conf",
                 "50-release.conf", "90-guard.conf"):
        (directory / name).write_bytes(b"[Service]\n")
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    monkeypatch.setattr(one, "require_privileged_private_root", lambda _: None)
    fake = FakeSystem(directory, ("argv[]=" + " ".join(argv) +
                                  " ; ignore_errors=no").encode())
    _, fake.candidate = p3.canonicalize(
        fake.original.decode(), app=app, releases=release.parent,
        release_sha=SHA, verified_release_sha=SHA)
    args = dict(system=fake, app=app, releases=release.parent,
                release_sha=SHA, verified_release_sha=SHA,
                private_root=private_root, dropin_dir=directory)
    return fake, args


def test_one_shot_switch_then_restores_exact_original(tmp_path, monkeypatch):
    fake, args = fixture(tmp_path, monkeypatch)
    original = fake.capture()
    assert one.run_once(**args) == "switch_and_restoration_verified"
    assert fake.restarts == 2 and fake.reloads == 2
    assert fake.capture() == original
    assert not (args["dropin_dir"] / p3.LATE_DROPIN).exists()
    snapshots = list(args["private_root"].glob("p3-*"))
    assert len(snapshots) == 1
    assert (snapshots[0] / "systemctl-cat.before").read_bytes() == original.unit_cat
    assert all(p.stat().st_mode & 0o077 == 0 for p in snapshots[0].iterdir())


@pytest.mark.parametrize("failure", ["fail_first", "interrupt_first"])
def test_restart_error_or_signal_always_restores(tmp_path, monkeypatch, failure):
    fake, args = fixture(tmp_path, monkeypatch)
    original = fake.capture()
    setattr(fake, failure, True)
    with pytest.raises((one.OperationalBlock, one.Interrupted)):
        one.run_once(**args)
    assert fake.restarts == 2
    assert fake.capture() == original
    assert not (args["dropin_dir"] / p3.LATE_DROPIN).exists()


def test_foreign_replacement_preserved_and_fails_closed(tmp_path, monkeypatch):
    fake, args = fixture(tmp_path, monkeypatch)
    fake.replace_first = True
    with pytest.raises(one.OperationalBlock, match="owned_dropin_rollback_failed"):
        one.run_once(**args)
    assert (args["dropin_dir"] / p3.LATE_DROPIN).read_bytes() == b"foreign"
    assert fake.restarts == 1


def test_preflight_failure_never_mutates_or_backs_up(tmp_path, monkeypatch):
    fake, args = fixture(tmp_path, monkeypatch)
    fake.health_ok = False
    with pytest.raises(one.OperationalBlock, match="preflight_runtime_unverified"):
        one.run_once(**args)
    assert fake.restarts == 0 and fake.reloads == 0
    assert not list(args["private_root"].glob("p3-*"))
    assert not (args["dropin_dir"] / p3.LATE_DROPIN).exists()


def test_no_root_privilege_and_no_direct_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(one.os, "geteuid", lambda: 1000)
    with pytest.raises(one.OperationalBlock, match="operator_privilege_unverified"):
        one.require_privileged_private_root(tmp_path)
    source = Path(one.__file__).read_text()
    assert "if __name__ == \"__main__\"" not in source
    assert "shell=True" not in source
