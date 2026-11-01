"""Safeguards for reversible Router release promotion."""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "router_release_promote.py"
spec = importlib.util.spec_from_file_location("router_release_promote", MODULE)
assert spec is not None and spec.loader is not None
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def test_candidate_pin_is_required(monkeypatch):
    assert promote.OVERRIDE.name == "zzzz-router-frozen-release.conf"
    monkeypatch.setattr(promote, "PINNED_SHA", "")
    with pytest.raises(promote.PromoteBlocked, match="candidate_sha_required"):
        promote.candidate_pin()
    monkeypatch.setattr(promote, "PINNED_SHA", "a" * 40)
    assert promote.candidate_pin() == "a" * 40


def test_promotion_forces_offline_and_blocks_egress():
    source = MODULE.read_text()
    assert "IPAddressDeny=any" in source
    assert "IPAddressAllow=localhost" in source
    assert "/usr/bin/env ROUTER_OFFLINE_ONLY=1" in source
    assert "postdeploy_offline_route=passed" in source
    assert "/v1/chat/completions" not in source


def test_original_argv_accepts_known_uvicorn_service():
    raw = ("{ path=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python ; "
           "argv[]=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python "
           "-m uvicorn router.api:app --host 127.0.0.1 --port 8010 ; "
           "ignore_errors=no ; start_time=[n/a] }")
    args = promote.original_argv(raw)
    assert args[-2:] == ["--port", "8010"]
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("127.0.0.1", "0.0.0.0"))
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("--port 8010", "--port 8080"))
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("router.api:app", "other.app:app"))


def test_existing_app_dir_is_replaced_without_duplication(monkeypatch, tmp_path):
    raw = ("{ path=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python ; "
           "argv[]=/home/ubuntu/Proyecto/Herramientas/Router/.venv/bin/python "
           "-m uvicorn router.api:app --host 127.0.0.1 --port 8010 "
           "--app-dir /home/ubuntu/Proyecto/Herramientas/Router ; "
           "ignore_errors=no ; start_time=[n/a] }")
    args = promote.original_argv(raw)
    release = tmp_path / "pinned-router-release"
    release.mkdir()
    monkeypatch.setattr(promote, "RELEASE", release)
    result = promote.candidate_argv(args)
    assert result.count("--app-dir") == 1
    assert result[result.index("--app-dir") + 1] == str(release)
    assert args[args.index("--app-dir") + 1] == str(promote.APP)
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("--app-dir /home/ubuntu/Proyecto/Herramientas/Router",
                                         "--app-dir /tmp/untrusted"))
    child = release / "src"
    child.mkdir()
    child_raw = raw.replace(
        "--app-dir /home/ubuntu/Proyecto/Herramientas/Router",
        "--app-dir /home/ubuntu/Proyecto/Herramientas/Router/src")
    child_argv = promote.original_argv(child_raw)
    assert promote.candidate_argv(child_argv)[-1] == str(child)
    relative_raw = raw.replace(
        "--app-dir /home/ubuntu/Proyecto/Herramientas/Router",
        "--app-dir src")
    assert promote.candidate_argv(promote.original_argv(relative_raw))[-1] == str(child)
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(relative_raw.replace("--app-dir src", "--app-dir ../outside"))
    with pytest.raises(promote.PromoteBlocked):
        promote.original_argv(raw.replace("--app-dir /home/ubuntu/Proyecto/Herramientas/Router",
                                         "--reload"))



def test_external_app_dir_remaps_only_when_it_cannot_shadow_live_router(monkeypatch, tmp_path):
    app = tmp_path / "checkout"
    (app / "router").mkdir(parents=True)
    (app / "router" / "api.py").write_text("app = object()\n")
    external = tmp_path / "external-import-root"
    external.mkdir()
    release = tmp_path / "candidate"
    release.mkdir()
    monkeypatch.setattr(promote, "APP", app)
    monkeypatch.setattr(promote, "RELEASE", release)
    raw = (
        f"{{ path={app}/.venv/bin/python ; argv[]={app}/.venv/bin/python "
        f"-m uvicorn router.api:app --host 127.0.0.1 --port 8010 "
        f"--app-dir {external} ; ignore_errors=no ; start_time=[n/a] }}"
    )
    argv = promote.original_argv(raw)
    candidate = promote.candidate_argv(argv)
    assert candidate[candidate.index("--app-dir") + 1] == str(release)
    assert argv[argv.index("--app-dir") + 1] == str(external)

    (external / "router").mkdir()
    (external / "router" / "api.py").write_text("app = 'different source'\n")
    with pytest.raises(promote.PromoteBlocked, match="service_app_dir_unsupported"):
        promote.original_argv(raw)
    with pytest.raises(promote.PromoteBlocked, match="service_app_dir_unsupported"):
        promote.candidate_argv(argv)




def test_external_clean_ancestral_checkout_is_preserved_and_remapped(monkeypatch, tmp_path):
    app = tmp_path / "checkout"
    (app / "router").mkdir(parents=True)
    (app / "router" / "api.py").write_text("app = 'tracked baseline'\n")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=app, text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("add", "router/api.py")
    git("commit", "-qm", "baseline")
    sha = git("rev-parse", "HEAD")
    external = tmp_path / "live-other-checkout"
    subprocess.run(["git", "clone", "-q", str(app), str(external)], check=True)
    release = tmp_path / "candidate"
    release.mkdir()
    monkeypatch.setattr(promote, "APP", app)
    monkeypatch.setattr(promote, "RELEASE", release)
    monkeypatch.setattr(promote, "PINNED_SHA", sha)
    raw = (
        f"{{ path={app}/.venv/bin/python ; argv[]={app}/.venv/bin/python "
        f"-m uvicorn router.api:app --host 127.0.0.1 --port 8010 "
        f"--app-dir {external} ; ignore_errors=no ; start_time=[n/a] }}"
    )
    original = promote.original_argv(raw)
    result = promote.candidate_argv(original)
    assert result[result.index("--app-dir") + 1] == str(release)
    assert original[original.index("--app-dir") + 1] == str(external)
    (external / "router" / "api.py").write_text("app = 'uncommitted override'\n")
    with pytest.raises(promote.PromoteBlocked, match="service_app_dir_unsupported"):
        promote.original_argv(raw)



def test_environment_file_parser_does_not_read_values():
    assert promote.environment_files("/etc/router-providers.env (ignore_errors=no)") == [
        "/etc/router-providers.env"
    ]
    with pytest.raises(promote.PromoteBlocked):
        promote.environment_files("garbled")
    with pytest.raises(promote.PromoteBlocked):
        promote.environment_files("/etc/../secret (ignore_errors=no)")



def test_late_release_dropin_preserves_existing_custom_override(monkeypatch):
    monkeypatch.setattr(
        promote, "command",
        lambda *args: "/etc/systemd/system/router.service.d/99-router-frozen-release.conf",
    )
    assert promote.dropin_precedence_safe()
    monkeypatch.setattr(
        promote, "command",
        lambda *args: "/etc/systemd/system/router.service.d/zzzzzz-later.conf",
    )
    assert not promote.dropin_precedence_safe()



def test_private_directory_rejects_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(promote.PromoteBlocked):
        promote.private_dir(alias)


def test_live_sqlite_snapshot_preserves_source(tmp_path):
    source = tmp_path / "live.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES ('unchanged')")
    copy = tmp_path / "snapshot.db"
    promote.online_snapshot(source, copy)
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("unchanged",)
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT value FROM sample").fetchone() == ("unchanged",)
    assert copy.stat().st_mode & 0o777 == 0o600


def test_no_destructive_git_or_database_restore():
    source = MODULE.read_text()
    for forbidden in ("git reset --hard", "git clean -fd", "git checkout -f",
                      "DROP TABLE", "rm -rf", "cp -f router.db"):
        assert forbidden not in source
    assert 'new_argv = argv + ["--app-dir", str(RELEASE)]' in source
    assert "automatic_rollback=" in source


def test_offline_postdeploy_verification_never_requests_inference(monkeypatch, capsys):
    monkeypatch.setenv("ROUTER_SERVICE_TOKEN", "synthetic-token")
    seen = []

    class Response(io.BytesIO):
        status = 200

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, json.loads(request.data), request.get_header("Authorization")))
        return Response(json.dumps({
            "ok": False, "error": "no_eligible_provider", "text": None, "attempts": [],
        }).encode())

    monkeypatch.setattr(promote.urllib.request, "urlopen", fake_urlopen)
    promote.inner_verify()
    assert len(seen) == 1
    url, payload, auth = seen[0]
    assert url == "http://127.0.0.1:8010/route"
    assert payload["application_name"] == "go-ute"
    assert auth == "Bearer synthetic-token"
    assert "postdeploy_offline_route=passed" in capsys.readouterr().out


def test_offline_postdeploy_verification_rejects_real_answer(monkeypatch):
    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        promote.urllib.request, "urlopen",
        lambda *args, **kwargs: Response(json.dumps({
            "ok": True, "error": None, "text": "real answer", "attempts": [],
        }).encode()),
    )
    with pytest.raises(promote.PromoteBlocked, match="postdeploy_offline_route_failed"):
        promote.inner_verify()


def test_candidate_data_isolated_from_live_backup_and_rollback(tmp_path):
    """A failed promotion must never migrate or overwrite the live SQLite/catalog."""
    work = tmp_path / "private"
    work.mkdir(mode=0o700)
    db = work / "predeploy-router.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE records (value TEXT)")
        conn.execute("INSERT INTO records VALUES ('baseline')")
    catalog = work / "predeploy-catalog.json"
    catalog.write_text('{"models": ["baseline"]}')
    candidate_db, candidate_catalog = promote.prepare_candidate_data(work)
    with sqlite3.connect(candidate_db) as conn:
        conn.execute("ALTER TABLE records ADD COLUMN candidate_only TEXT")
        conn.execute("INSERT INTO records VALUES ('candidate', 'new')")
    candidate_catalog.write_text('{"models": ["candidate"]}')
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM records").fetchall() == [("baseline",)]
        assert [column[1] for column in conn.execute("PRAGMA table_info(records)")] == ["value"]
    assert catalog.read_text() == '{"models": ["baseline"]}'
    assert candidate_db.stat().st_mode & 0o777 == 0o600
    assert candidate_catalog.stat().st_mode & 0o777 == 0o600
    with pytest.raises(promote.PromoteBlocked, match="candidate_data_snapshot_unavailable"):
        promote.prepare_candidate_data(work)



def test_private_backup_uses_effective_process_data_paths_not_provider_envfiles(tmp_path):
    db = tmp_path / "live.db"
    db.write_bytes(b"sqlite-fixture")
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}")
    raw = (b"PROVIDER_API_KEY=do-not-copy-to-private-unit\0"
           b"ROUTER_DB_FILE=live.db\0"
           b"ROUTER_CATALOG_FILE=catalog.json\0")
    assert promote.effective_data_paths_from_environ(raw, tmp_path) == (
        str(db), str(catalog))
    with pytest.raises(promote.PromoteBlocked, match="effective_data_paths_unavailable"):
        promote.effective_data_paths_from_environ(b"ROUTER_DB_FILE=live.db\0", tmp_path)
    alias = tmp_path / "alias.json"
    alias.symlink_to(catalog)
    with pytest.raises(promote.PromoteBlocked, match="effective_data_paths_unsafe"):
        promote.effective_data_paths_from_environ(
            b"ROUTER_DB_FILE=live.db\0ROUTER_CATALOG_FILE=alias.json\0", tmp_path)
    source = MODULE.read_text()
    assert 'if mode == "backup":' in source
    assert '"--setenv=ROUTER_DB_FILE=" + db_path' in source
    assert '"--setenv=ROUTER_CATALOG_FILE=" + catalog_path' in source


def test_external_interpreter_promotes_only_as_reversible_canonical_override(monkeypatch, tmp_path):
    app = tmp_path / "checkout"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "router").mkdir()
    (app / "router/api.py").write_text("app = object()\\n")
    canonical = app / ".venv/bin/python"
    canonical.write_text("synthetic canonical python")
    canonical.chmod(0o755)
    external_root = tmp_path / "external-checkout"
    (external_root / "router").mkdir(parents=True)
    (external_root / "router/api.py").write_text("app = object()\n")
    (external_root / ".venv/bin").mkdir(parents=True)
    interpreter = external_root / ".venv/bin/python"
    interpreter.write_text("synthetic interpreter")
    interpreter.chmod(0o755)
    external_binary = external_root / ".venv/bin/uvicorn"
    external_binary.write_text(
        f"#!{interpreter}\n"
        "import sys\n"
        "from uvicorn.main import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n"
    )
    external_binary.chmod(0o755)
    release = tmp_path / "frozen"
    release.mkdir()
    monkeypatch.setattr(promote, "APP", app)
    monkeypatch.setattr(promote, "RELEASE", release)
    monkeypatch.setattr(
        promote, "clean_known_external_checkout",
        lambda root: root == external_root,
    )
    raw = (
        f"{{ path={external_binary} ; argv[]={external_binary} "
        f"router.api:app --host 127.0.0.1 --port 8010 "
        f"--app-dir {external_root} ; ignore_errors=no }}"
    )
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.original_argv(raw)
    normalized = promote.promotion_argv(raw)
    assert normalized[:3] == [str(canonical), "-m", "uvicorn"]
    candidate = promote.candidate_argv(normalized)
    assert candidate[:3] == normalized[:3]
    assert candidate[candidate.index("--app-dir") + 1] == str(release)
    assert candidate[candidate.index("--host") + 1] == "127.0.0.1"
    assert candidate[candidate.index("--port") + 1] == "8010"

    monkeypatch.setattr(promote, "clean_known_external_checkout", lambda root: False)
    with pytest.raises(promote.PromoteBlocked, match="service_app_dir_unsupported"):
        promote.promotion_argv(raw)
    with pytest.raises(promote.PromoteBlocked):
        promote.promotion_argv(raw.replace("--port 8010", "--port 8080"))
    with pytest.raises(promote.PromoteBlocked):
        promote.promotion_argv(raw.replace("127.0.0.1", "0.0.0.0"))
    with pytest.raises(promote.PromoteBlocked):
        promote.promotion_argv(raw.replace("router.api:app", "other.api:app"))
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw.replace(str(external_binary), "/bin/sh"))


def test_external_interpreter_rejects_unverified_or_symlinked_binary(monkeypatch, tmp_path):
    app = tmp_path / "app"
    (app / ".venv/bin").mkdir(parents=True)
    (app / ".venv/bin/python").write_text("synthetic")
    monkeypatch.setattr(promote, "APP", app)
    external_root = tmp_path / "external"
    external_root.mkdir()
    binary = tmp_path / "uvicorn"
    binary.write_text("synthetic")
    alias = tmp_path / "alias"
    alias.symlink_to(binary)
    raw = (
        f"argv[]={alias} router.api:app --host 127.0.0.1 --port 8010 "
        f"--app-dir {external_root} ; ignore_errors=no"
    )
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw)


def test_rollback_verifies_original_effective_exec_before_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(promote, "run", lambda *args: calls.append(args))
    monkeypatch.setattr(promote, "wait_health", lambda: True)
    monkeypatch.setattr(promote, "command", lambda *args: "original-exec")
    assert promote.rollback("original-exec")
    assert any("daemon-reload" in args for args in calls)
    assert any("restart" in args for args in calls)
    calls.clear()
    assert not promote.rollback("different-exec")
    assert not any("restart" in args for args in calls)



def test_release_override_preflight_rejects_existing_file_dangling_link_and_sudo_error(monkeypatch):
    def check(file_status, symlink_status, first_probe=0, last_probe=0,
              inverse_file_status=0, inverse_symlink_status=0):
        observed = []
        def fake_run(args, **kwargs):
            inverse = args[3] == "!"
            flag, target = args[-2:]
            observed.append((("!" if inverse else "") + flag, target))
            if target == "/":
                status = first_probe if observed.count(("-e", "/")) == 1 else last_probe
            elif inverse:
                status = inverse_file_status if flag == "-e" else inverse_symlink_status
            else:
                status = file_status if flag == "-e" else symlink_status
            return subprocess.CompletedProcess(args, status)
        monkeypatch.setattr(promote.subprocess, "run", fake_run)
        return promote.release_override_absent(), observed

    initial = [("-e", "/")]
    file_check = initial + [("-e", str(promote.OVERRIDE))]
    inverse_file_check = file_check + [("!-e", str(promote.OVERRIDE))]
    symlink_check = inverse_file_check + [("-L", str(promote.OVERRIDE))]
    inverse_symlink_check = symlink_check + [("!-L", str(promote.OVERRIDE))]
    complete = inverse_symlink_check + [("-e", "/")]
    assert check(1, 1) == (True, complete)
    assert check(0, 1) == (False, file_check)
    assert check(1, 0) == (False, symlink_check)
    assert check(2, 1) == (False, file_check)
    assert check(1, 2) == (False, symlink_check)
    # A selective sudoers denial on the protected path cannot look absent.
    assert check(1, 1, inverse_file_status=1) == (False, inverse_file_check)
    assert check(1, 1, inverse_symlink_status=1) == (False, inverse_symlink_check)
    assert check(1, 1, inverse_file_status=2) == (False, inverse_file_check)
    assert check(1, 1, inverse_symlink_status=2) == (False, inverse_symlink_check)
    assert check(1, 1, first_probe=1) == (False, initial)
    assert check(1, 1, last_probe=1) == (False, complete)


def test_dropin_install_uses_atomic_create_without_replacing_existing_override():
    source = MODULE.read_text()
    assert 'run("sudo", "-n", "ln", temp_override, str(OVERRIDE))' in source
    assert 'run("sudo", "-n", "mv", "-T", temp_override, str(OVERRIDE))' not in source
    assert 'run("sudo", "-n", "rm", "-f", temp_override)' in source


def test_external_launcher_rejects_arbitrary_wrapper_and_foreign_python(monkeypatch, tmp_path):
    app = tmp_path / "canonical"
    (app / ".venv/bin").mkdir(parents=True)
    (app / "router").mkdir()
    (app / "router/api.py").write_text("app = object()\n")
    (app / ".venv/bin/python").write_text("synthetic")
    (app / ".venv/bin/python").chmod(0o755)
    monkeypatch.setattr(promote, "APP", app)
    root = tmp_path / "external"
    (root / ".venv/bin").mkdir(parents=True)
    (root / "router").mkdir()
    (root / "router/api.py").write_text("app = object()\n")
    interpreter = root / ".venv/bin/python"
    interpreter.write_text("synthetic")
    interpreter.chmod(0o755)
    launcher = root / ".venv/bin/uvicorn"
    launcher.write_text(
        f"#!{interpreter}\n"
        "import sys\n"
        "from uvicorn.main import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(promote, "clean_known_external_checkout", lambda path: path == root)
    def raw(executable, args=""):
        return (
            f"argv[]={executable} {args}router.api:app --host 127.0.0.1 "
            f"--port 8010 --app-dir {root} ; ignore_errors=no"
        )
    assert promote.promotion_argv(raw(launcher))[:3] == [
        str(app / ".venv/bin/python"), "-m", "uvicorn"
    ]
    launcher.write_text(f"#!{interpreter}\nimport os\nos.system('echo unexpected')\n")
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw(launcher))
    launcher.write_text(f"#!/wrong/interpreter\nfrom uvicorn.main import main\n")
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw(launcher))
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw(interpreter, "-m uvicorn "))
    launcher.chmod(0o644)
    with pytest.raises(promote.PromoteBlocked, match="service_interpreter_unsupported"):
        promote.promotion_argv(raw(launcher))
