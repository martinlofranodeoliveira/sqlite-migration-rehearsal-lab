"""Backup's independent page/busy bounds, bounded hashing and cleanup failures."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlite_migration_rehearsal.demo import build_reversible_users_status_manifest, create_users_basic_baseline
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.errors import RehearsalError
from sqlite_migration_rehearsal.models import RehearsalLimits
from sqlite_migration_rehearsal.workspace import (
    backup_source_to_workspace, compute_file_sha256, open_readonly_sqlite,
)


class BackupTests(unittest.TestCase):
    def test_hash_rejects_invalid_budgets_before_opening_input(self):
        for maximum in (-1, True, 1.5, 16 * 1024 * 1024 + 1):
            with self.subTest(maximum=maximum):
                with patch("sqlite_migration_rehearsal.workspace.regular_reader") as reader:
                    with self.assertRaises(RehearsalError):
                        compute_file_sha256("synthetic-not-opened", max_bytes=maximum)
                    reader.assert_not_called()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = create_users_basic_baseline(self.root / "source.sqlite3")
        self.original = self.db.read_bytes()

    def test_source_connection_is_read_only_and_destination_exclusive(self):
        conn = open_readonly_sqlite(self.db)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute("DELETE FROM users")
        finally:
            conn.close()
        target = self.root / "existing"
        target.write_bytes(b"synthetic caller bytes")
        with self.assertRaises(RehearsalError):
            backup_source_to_workspace(self.db, target, limits=RehearsalLimits())
        self.assertEqual(target.read_bytes(), b"synthetic caller bytes")
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_hash_byte_budget_includes_eof_probe(self):
        self.assertEqual(compute_file_sha256(self.db, max_bytes=len(self.original)),
                         compute_file_sha256(self.db))
        with self.assertRaises(RehearsalError) as caught:
            compute_file_sha256(self.db, max_bytes=len(self.original) - 1)
        self.assertEqual(caught.exception.diagnostic.code.value, "RESOURCE_LIMIT_EXCEEDED")

    def test_progress_aborts_busy_growth_and_restart_cycles_and_engine_cleans_partial(self):
        real_open = open_readonly_sqlite
        for mode in ("busy", "growth", "restart"):
            closed = []

            class SourceProxy:
                def __init__(self, path):
                    self.conn = real_open(path)

                def execute(self, sql):
                    return self.conn.execute(sql)

                def close(self):
                    self.conn.close()
                    closed.append(True)

                def backup(self, destination, *, pages, progress, sleep):
                    self_outer.assertEqual(pages, 1)
                    self_outer.assertEqual(sleep, 0.0)
                    destination.execute("CREATE TABLE partial(id INTEGER PRIMARY KEY)")
                    if mode == "growth":
                        progress(sqlite3.SQLITE_OK, 100000, 100000)
                    else:
                        for unused in range(5000):
                            progress(sqlite3.SQLITE_BUSY if mode == "busy" else sqlite3.SQLITE_OK, 1, 2)
                    self_outer.fail("Backup should have been interrupted by its independent budget")

            self_outer = self
            with self.subTest(mode=mode), patch("sqlite_migration_rehearsal.workspace.open_readonly_sqlite", SourceProxy):
                result = rehearse_migration(self.db, build_reversible_users_status_manifest(), workspace_parent_dir=self.root)
                self.assertEqual(result.failure.code.value, "RESOURCE_LIMIT_EXCEEDED")
                self.assertTrue(result.workspace_cleaned)
                self.assertTrue(result.source_preserved)
                self.assertTrue(closed)
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source.sqlite3"])
                self.assertEqual(self.db.read_bytes(), self.original)

    def test_live_wal_is_included_by_backup_and_oversized_sidecars_are_rejected(self):
        wal_db = self.root / "wal.sqlite3"
        conn = sqlite3.connect(wal_db)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            with conn:
                conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
                conn.executemany("INSERT INTO users VALUES (?,?)", [(1, "Ada"), (2, "Lin")])
            result = rehearse_migration(wal_db, build_reversible_users_status_manifest(), workspace_parent_dir=self.root)
            self.assertTrue(result.success, result.failure)
            self.assertEqual(result.baseline_snapshot.total_rows, 2)
            # Main-file size alone is insufficient: live WAL plus SHM exceeds 8 KiB.
            result = rehearse_migration(wal_db, build_reversible_users_status_manifest(),
                                        limits=RehearsalLimits(max_baseline_bytes=8192), workspace_parent_dir=self.root)
            self.assertEqual(result.failure.code.value, "RESOURCE_LIMIT_EXCEEDED")
        finally:
            conn.close()

    def test_cleanup_error_is_sanitized_and_never_reported_as_success(self):
        with patch("sqlite_migration_rehearsal.engine.shutil.rmtree", side_effect=OSError("private path")):
            result = rehearse_migration(self.db, build_reversible_users_status_manifest(), workspace_parent_dir=self.root)
        self.assertFalse(result.success)
        self.assertFalse(result.workspace_cleaned)
        self.assertNotIn("private path", result.failure.message)
        self.assertEqual(self.db.read_bytes(), self.original)
