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
        if not sep or not first.startswith("#!"):
            return False
        # Oracle read-only probe #41329983185 confirmed an executable
        # checkout-venv Python symlink to system Python, but not the literal
        # '.venv/bin/python' shebang. Accept only that standard pip variant.
        directive = first[2:].strip()
        if not directive or any(ch.isspace() for ch in directive):
            return False
        interpreter = Path(directive)
        if (interpreter.parent != root / ".venv/bin"
                or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", interpreter.name)
                or not interpreter.is_file() or not os.access(interpreter, os.X_OK)):
            return False
        resolved = interpreter.resolve(strict=True)
        if interpreter.is_symlink() and not (
            resolved.parent in (Path("/usr/bin"), Path("/usr/local/bin"))
            and re.fullmatch(r"python3(?:\.\d+)?", resolved.name)
        ):
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



def classify_nested_shebang(executable: Path, *, checkout: Path, release: Path) -> str:
    """Read-only P3 evidence probe. Return ONLY fixed labels, never paths or data.

    This does not authorize canonicalization: a verified shebang alone cannot
    establish that the launcher body matches the supported pip wrapper.
    """
    expected = {
        checkout / ".venv/bin/uvicorn": "checkout",
        release / ".venv/bin/uvicorn": "release",
    }
    if executable not in expected:
        return "launcher_outside_expected"
    try:
        if (executable.is_symlink() or not executable.is_file()
                or not os.access(executable, os.X_OK)):
            return "launcher_unsafe"
        with executable.open("rb") as stream:
            first = stream.readline(512)
        if not first.startswith(b"#!") or not first.endswith(b"\n"):
            return "shebang_missing_or_long"
        directive = first[2:-1].decode("utf-8").strip()
        if not directive or any(c.isspace() for c in directive):
            return "shebang_arguments_unsupported"
        interpreter = Path(directive)
        if not interpreter.is_absolute():
            return "shebang_not_absolute"
        roots = {
            checkout / ".venv/bin": "checkout",
            release / ".venv/bin": "release",
        }
        scope = roots.get(interpreter.parent)
        if scope is None or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", interpreter.name):
            return "interpreter_outside_expected_venv"
        if not interpreter.is_file():
            return "interpreter_missing"
        if not os.access(interpreter, os.X_OK):
            return "interpreter_not_executable"
        resolved = interpreter.resolve(strict=True)
        if interpreter.is_symlink() and not (
            resolved.parent in (Path("/usr/bin"), Path("/usr/local/bin"))
            and re.fullmatch(r"python3(?:\.\d+)?", resolved.name)
        ):
            return "interpreter_symlink_untrusted"
        return "expected_" + scope + "_venv_python_executable"
    except (OSError, UnicodeError, RuntimeError):
        return "probe_unreadable"


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



def verify_dropin_precedence(effective_paths: list[str], *, expected_count: int = 4) -> None:
    """Preflight only: reject unverified systemd drop-in ordering.

    The caller must obtain the *effective* DropInPaths from systemd immediately
    before a future authorized switch. This function never reads Oracle or
    installs a file; an earlier inventory cannot authorize a later write.
    """
    if len(effective_paths) != expected_count:
        raise MigrationBlocked("dropin_inventory_changed")
    seen: set[str] = set()
    for raw in effective_paths:
        path = Path(raw)
        name = path.name
        if (not path.is_absolute() or ".." in path.parts
                or path.parent.name != "router.service.d"
                or not name.endswith(".conf")
                or not TOKEN.fullmatch(name) or name in seen):
            raise MigrationBlocked("dropin_inventory_unsafe")
        if name >= LATE_DROPIN:
            raise MigrationBlocked("dropin_precedence_unverified")
        seen.add(name)


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
    # Pin the backup directory to an open descriptor. Checking the pathname
    # and then opening children through that pathname permits a symlink swap
    # between the check and the write (and could disclose systemctl-cat).
    try:
        directory_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise MigrationBlocked("backup_directory_unsafe") from None
    try:
        info = os.fstat(directory_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise MigrationBlocked("backup_directory_unsafe")
        for name, data in (("systemctl-cat.before", unit_bytes), ("release-dropin.before", dropin_bytes)):
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


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
    """Quarantine before deleting: never unlink a concurrently substituted drop-in.

    POSIX offers no compare-and-unlink primitive. An identity check followed by
    unlink is racy. Move the entry into a fresh private directory via pinned
    directory FDs, then inspect *the moved entry*. If it is foreign, hard-link
    it back with create-if-absent semantics (including symlinks) and keep the
    quarantine if a concurrent writer already occupied the original name.
    In either case a foreign entry is never unlinked.
    """
    try:
        if identity(directory / LATE_DROPIN) != owned:
            return False
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (OSError, MigrationBlocked):
        return False
    quarantine = ".router-rollback-" + os.urandom(12).hex()
    qfd = None
    made = False
    held = False
    try:
        os.mkdir(quarantine, mode=0o700, dir_fd=fd)
        made = True
        qfd = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.rename(LATE_DROPIN, "held", src_dir_fd=fd, dst_dir_fd=qfd)
        held = True
        # Read through the pinned quarantine FD; rename cannot be paired with
        # a pre-rename identity test atomically, so decide only AFTER the move.
        try:
            h = os.open("held", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=qfd)
            with os.fdopen(h, "rb") as stream:
                st1 = os.fstat(stream.fileno())
                if not stat.S_ISREG(st1.st_mode):
                    raise MigrationBlocked("quarantined_entry_not_regular")
                digest = hashlib.sha256(stream.read()).hexdigest()
                st2 = os.fstat(stream.fileno())
                if (st1.st_dev, st1.st_ino, st1.st_size, st1.st_ctime_ns) != (
                    st2.st_dev, st2.st_ino, st2.st_size, st2.st_ctime_ns
                ):
                    raise MigrationBlocked("quarantined_entry_changed")
                # Rename may change ctime, so match inode, device, size and hash.
                is_ours = (st2.st_dev, st2.st_ino, st2.st_size, digest) == (
                    owned[0], owned[1], owned[3], owned[4]
                )
        except (OSError, MigrationBlocked):
            is_ours = False
        if not is_ours:
            try:
                # Hard-link the foreign entry without overwriting an entry
                # another writer may have created in the meantime.
                os.link("held", LATE_DROPIN, src_dir_fd=qfd,
                        dst_dir_fd=fd, follow_symlinks=False)
            except OSError:
                # Preserve foreign content in quarantine for recovery.
                return False
            os.unlink("held", dir_fd=qfd)
            held = False
            return False
        os.unlink("held", dir_fd=qfd)
        held = False
        return True
    except OSError:
        return False
    finally:
        if qfd is not None:
            os.close(qfd)
        if made and not held:
            try:
                os.rmdir(quarantine, dir_fd=fd)
            except OSError:
                pass
        os.close(fd)


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
        verify_dropin_precedence([
            "/etc/systemd/system/router.service.d/10-runtime.conf",
            "/etc/systemd/system/router.service.d/30-offline.conf",
            "/etc/systemd/system/router.service.d/50-release.conf",
            "/etc/systemd/system/router.service.d/90-guard.conf",
        ])
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
