"""Offline-only backup/restore rehearsal for the Router release candidate.

Exercises the existing snapshot implementations with synthetic WAL-mode SQLite.
Never touches Oracle, live databases, credentials, providers, or the network.
"""
import sqlite3
import stat

import pytest

from scripts import router_release_promote as promote
from scripts import router_release_stage as stage


pytestmark = pytest.mark.strict_billing


@pytest.mark.parametrize(
    "snapshot",
    [stage.sqlite_snapshot, promote.online_snapshot],
    ids=["stage", "promote"],
)
def test_live_wal_snapshot_restores_consistent_committed_data(snapshot, tmp_path):
    source = tmp_path / "synthetic-live.db"
    backup = tmp_path / "private-backup.db"
    restored = tmp_path / "synthetic-restored.db"

    # Keep the source open in WAL mode to simulate a running SQLite writer.
    with sqlite3.connect(source) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        writer.execute("INSERT INTO entries(value) VALUES ('before-backup')")
        writer.commit()

        snapshot(source, backup)
        assert backup.is_file()
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

        # Changes after the snapshot must not appear in a restored copy.
        writer.execute("INSERT INTO entries(value) VALUES ('after-backup')")
        writer.commit()

        with sqlite3.connect(backup) as saved, sqlite3.connect(restored) as target:
            assert saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            saved.backup(target)

    with sqlite3.connect(restored) as restored_db:
        assert restored_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored_db.execute("SELECT value FROM entries ORDER BY id").fetchall() == [
            ("before-backup",)
        ]
    with sqlite3.connect(source) as original:
        assert original.execute("SELECT count(*) FROM entries").fetchone()[0] == 2


@pytest.mark.parametrize(
    "snapshot,error",
    [
        (stage.sqlite_snapshot, stage.StageBlocked),
        (promote.online_snapshot, promote.PromoteBlocked),
    ],
    ids=["stage", "promote"],
)
def test_snapshot_rejects_symlink_database_path(snapshot, error, tmp_path):
    real = tmp_path / "synthetic-real.db"
    with sqlite3.connect(real) as connection:
        connection.execute("CREATE TABLE entries (id INTEGER)")
    link = tmp_path / "synthetic-link.db"
    link.symlink_to(real)
    with pytest.raises(error):
        snapshot(link, tmp_path / "must-not-exist.db")
    assert not (tmp_path / "must-not-exist.db").exists()


def test_stage_snapshot_skips_absent_database_without_creating_backup(tmp_path):
    destination = tmp_path / "absent-backup.db"
    assert stage.sqlite_snapshot(tmp_path / "absent-source.db", destination) is False
    assert not destination.exists()
