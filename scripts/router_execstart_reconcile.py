#!/usr/bin/env python3
"""P3: fail-closed, isolated rehearsal for an env-wrapped Router ExecStart.

This module NEVER calls sudo, systemctl restart, or writes to Oracle. It
constructs a late, removable systemd drop-in from a verified 12-token service
command and rehearses atomic install/identity-bound rollback in a temp dir.
A production adapter requires a separate reviewed authorization.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

EXPECTED_ARGS = 12
ALLOWED_ENV = {"ROUTER_OFFLINE_ONLY", "ROUTER_DB_FILE", "ROUTER_CATALOG_FILE"}
TOKEN = re.compile(r"[A-Za-z0-9_./:=+-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
LATE_DROPIN = "zzzzz-router-canonical-execstart.conf"


class MigrationBlocked(Exception):
    pass


def exec_argv(raw: str) -> list[str]:
    match = re.search(r"argv\[\]=(.*?)\s+;\s+ignore_errors=", raw)
    if not match:
        raise MigrationBlocked("execstart_unparseable")
    try:
        argv = shlex.split(match.group(1))
    except ValueError:
        raise MigrationBlocked("execstart_unparseable") from None
    if len(argv) != EXPECTED_ARGS or any(not TOKEN.fullmatch(a) for a in argv):
        raise MigrationBlocked("execstart_shape_unverified")
    return argv


def pip_uvicorn_launcher(executable: Path, root: Path) -> bool:
    """Recognize pip's standard launcher, never execute an unknown wrapper."""
    expected = root / ".venv/bin/uvicorn"
    python = root / ".venv/bin/python"
    try:
        if (executable != expected or executable.is_symlink()
                or executable.resolve(strict=True) != expected
                or not executable.is_file() or not os.access(executable, os.X_OK)
                or not python.is_file() or not os.access(python, os.X_OK)):
            return False
        content = executable.read_bytes()
        if len(content) > 2048 or b"\0" in content:
            return False
        first, sep, body = content.decode("utf-8").partition("\n")
        if not sep or first != "#!" + str(python):
            return False
        modern = (
            "import sys\n"
            "from uvicorn.main import main\n"
            "if __name__ == '__main__':\n"
            "    if sys.argv[0].endswith('.exe'):\n"
            "        sys.argv[0] = sys.argv[0][:-4]\n"
            "    sys.exit(main())\n"
        )
        legacy = (
            "import re\nimport sys\nfrom uvicorn.main import main\n"
            "if __name__ == '__main__':\n"
            "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
            "    sys.exit(main())\n"
        )
        return body.strip() + "\n" in (
            modern, legacy, "# -*- coding: utf-8 -*-\n" + modern,
            "# -*- coding: utf-8 -*-\n" + legacy,
        )
    except (OSError, UnicodeError, RuntimeError):
        return False


def canonicalize(
    raw: str, *, app: Path, releases: Path, release_sha: str,
    verified_release_sha: str,
) -> tuple[list[str], list[str]]:
    """Preserve every env assignment and CLI flag; replace only pip uvicorn.

    The live release must already be identified by an independent, read-only
    git SHA check. An external_other classification alone is not evidence
    that the first executable is /usr/bin/env.
    """
    if not SHA.fullmatch(release_sha) or verified_release_sha != release_sha:
        raise MigrationBlocked("release_sha_unverified")
    argv = exec_argv(raw)
    if argv[0] != "/usr/bin/env":
        raise MigrationBlocked("env_wrapper_unverified")
    env: dict[str, str] = {}
    index = 1
    while index < len(argv) and "=" in argv[index]:
        key, value = argv[index].split("=", 1)
        if key not in ALLOWED_ENV or key in env or not value:
            raise MigrationBlocked("env_assignment_unsupported")
        env[key] = value
        index += 1
    if set(env) != ALLOWED_ENV or env["ROUTER_OFFLINE_ONLY"] != "1":
        raise MigrationBlocked("offline_or_data_environment_unverified")
    if not all(Path(env[k]).is_absolute() for k in ("ROUTER_DB_FILE", "ROUTER_CATALOG_FILE")):
        raise MigrationBlocked("data_paths_not_absolute")
    if index >= len(argv):
        raise MigrationBlocked("nested_launcher_missing")
    inner = Path(argv[index])
    expected_release = releases / release_sha
    if (expected_release.is_symlink() or not expected_release.is_dir()
            or expected_release.resolve(strict=True) != expected_release):
        raise MigrationBlocked("release_directory_unsafe")
    if argv.count("router.api:app") != 1 or argv.count("--app-dir") != 1:
        raise MigrationBlocked("router_import_unverified")
    app_index = argv.index("--app-dir")
    if app_index + 1 >= len(argv) or argv[app_index + 1] != str(expected_release):
        raise MigrationBlocked("release_app_dir_unverified")
    if "--reload" in argv or argv.count("--host") != 1 or argv.count("--port") != 1:
        raise MigrationBlocked("service_flags_unverified")
    try:
        if argv[argv.index("--host") + 1] != "127.0.0.1" or argv[argv.index("--port") + 1] != "8010":
            raise MigrationBlocked("service_bind_unverified")
    except IndexError:
        raise MigrationBlocked("service_flags_unverified") from None
    canonical = app / ".venv/bin/python"
    try:
        resolved_python = canonical.resolve(strict=True)
        if not canonical.is_file() or not os.access(canonical, os.X_OK):
            raise MigrationBlocked("canonical_python_unavailable")
        # Standard venv Python is commonly a symlink to the system interpreter.
        # Permit only that narrow, known symlink target; not an arbitrary binary.
        if canonical.is_symlink() and not (
            resolved_python.parent in (Path("/usr/bin"), Path("/usr/local/bin"))
            and re.fullmatch(r"python3(?:\.\d+)?", resolved_python.name)
        ):
            raise MigrationBlocked("canonical_python_unverified_symlink")
    except (OSError, RuntimeError):
        raise MigrationBlocked("canonical_python_unavailable") from None
    if inner == canonical and argv[index + 1:index + 3] == ["-m", "uvicorn"]:
        return argv, argv.copy()
    if inner != app / ".venv/bin/uvicorn" and inner != expected_release / ".venv/bin/uvicorn":
        raise MigrationBlocked("nested_launcher_unsupported")
    source_root = app if inner == app / ".venv/bin/uvicorn" else expected_release
    if not pip_uvicorn_launcher(inner, source_root):
        raise MigrationBlocked("nested_launcher_unverified")
    candidate = argv[:index] + [str(canonical), "-m", "uvicorn"] + argv[index + 1:]
    if candidate[:index] != argv[:index] or candidate[index + 3:] != argv[index + 1:]:
        raise MigrationBlocked("argument_preservation_failed")
    return argv, candidate


def render_dropin(argv: list[str]) -> bytes:
    if not argv or any(not TOKEN.fullmatch(arg) for arg in argv):
        raise MigrationBlocked("dropin_arguments_unsafe")
    return ("[Service]\nExecStart=\nExecStart=" + " ".join(argv) + "\n").encode()


def identity(path: Path) -> tuple[int, int, int, int, str]:
    """Bind ownership to the opened regular file and its contents.

    Device/inode alone is insufficient: an unlinked inode may be reused for
    a foreign replacement. O_NOFOLLOW also rejects symlinks.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise MigrationBlocked("dropin_identity_unsafe")
            digest = hashlib.sha256(handle.read()).hexdigest()
            after = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino, before.st_ctime_ns, before.st_size) != (
                after.st_dev, after.st_ino, after.st_ctime_ns, after.st_size
            ):
                raise MigrationBlocked("dropin_identity_changed")
            return (after.st_dev, after.st_ino, after.st_ctime_ns, after.st_size, digest)
    except OSError:
        raise MigrationBlocked("dropin_identity_missing") from None


def backup_original(unit_bytes: bytes, dropin_bytes: bytes, private: Path) -> None:
    """Retain original unit/drop-in without exposing their contents in logs."""
    if private.is_symlink() or not private.is_dir() or private.stat().st_mode & 0o077:
        raise MigrationBlocked("backup_directory_unsafe")
    for name, data in (("systemctl-cat.before", unit_bytes), ("release-dropin.before", dropin_bytes)):
        path = private / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


def install_late_dropin(directory: Path, content: bytes) -> tuple[int, int, int, int, str]:
    """Atomic create-if-absent, no overwrite; isolated filesystem operation."""
    if directory.is_symlink() or not directory.is_dir():
        raise MigrationBlocked("dropin_directory_unsafe")
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pending = ".router-execstart-pending-" + str(os.getpid())
    try:
        out = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o644, dir_fd=fd)
        with os.fdopen(out, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        source = directory / pending
        source_identity = identity(source)
        os.link(pending, LATE_DROPIN, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        installed = identity(directory / LATE_DROPIN)
        # Hardlink creation changes ctime; compare stable inode and digest.
        if installed[:2] != source_identity[:2] or installed[-1] != source_identity[-1]:
            raise MigrationBlocked("installed_identity_changed")
        # Removing the pending hardlink changes ctime again. Capture the final
        # owned identity only AFTER removing it, for reliable rollback checks.
        os.unlink(pending, dir_fd=fd)
        pending = None
        return identity(directory / LATE_DROPIN)
    except FileExistsError:
        raise MigrationBlocked("dropin_already_exists") from None
    finally:
        try:
            if pending is not None:
                os.unlink(pending, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def rollback_late_dropin(directory: Path, owned: tuple[int, int, int, int, str]) -> bool:
    """Remove only the inode created by us. Caller must verify effective ExecStart."""
    path = directory / LATE_DROPIN
    try:
        if identity(path) != owned:
            return False
        path.unlink()
        return True
    except (OSError, MigrationBlocked):
        return False


def rehearse() -> None:
    """Pure local rehearsal: no Oracle, no service restart and no network."""
    with tempfile.TemporaryDirectory(prefix="router-execstart-rehearsal-") as root:
        work = Path(root)
        private = work / "private"
        private.mkdir(mode=0o700)
        original = b"[Service]\nExecStart=/original\n"
        backup_original(original, original, private)
        drops = work / "dropins"
        drops.mkdir()
        payload = render_dropin(["/usr/bin/env", "ROUTER_OFFLINE_ONLY=1", "/bin/true"])
        owned = install_late_dropin(drops, payload)
        if (drops / LATE_DROPIN).read_bytes() != payload:
            raise MigrationBlocked("rehearsal_install_mismatch")
        if not rollback_late_dropin(drops, owned) or (drops / LATE_DROPIN).exists():
            raise MigrationBlocked("rehearsal_rollback_failed")
        if (private / "release-dropin.before").read_bytes() != original:
            raise MigrationBlocked("rehearsal_backup_changed")
    print("isolated_execstart_rehearsal=passed")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--rehearse"]:
        raise SystemExit("Only --rehearse is supported; production apply is intentionally disabled")
    rehearse()
