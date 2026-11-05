"""Independent hand-authored workflow oracles, including cascade-loss regression."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlite_migration_rehearsal.demo import create_users_basic_baseline
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.manifest import load_manifest_file, parse_manifest_dict
from sqlite_migration_rehearsal.models import ColumnSpec, ForeignKeySpec, IndexSpec, TableCatalog, TypedValue
from sqlite_migration_rehearsal.workspace import compute_file_sha256

ROOT = Path(__file__).resolve().parent.parent


class RehearsalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rehearsal_test_")
        self.addCleanup(self.temporary.cleanup)
        self.temp_dir = Path(self.temporary.name)

    def test_reversible_users_migration_against_hand_authored_states(self):
        baseline = create_users_basic_baseline(self.temp_dir / "users.sqlite3")
        original = baseline.read_bytes()
        fingerprint = compute_file_sha256(baseline)
        manifest = load_manifest_file(ROOT / "fixtures/manifests/users_add_status_reversible.json")
        result = rehearse_migration(baseline, manifest, workspace_parent_dir=self.temp_dir)
        self.assertTrue(result.success, result.failure)
        self.assertEqual(result.status, "passed")
        self.assertIsNone(result.failure)
        self.assertTrue(result.source_preserved)
        self.assertTrue(result.workspace_cleaned)
        self.assertEqual(result.source_sha256_before, fingerprint)
        self.assertEqual(result.source_sha256_after, fingerprint)
        self.assertEqual(baseline.read_bytes(), original)
        columns = (ColumnSpec(0, "id", "INTEGER", False, None, 1),
                   ColumnSpec(1, "name", "TEXT", False, None, 0))
        base_catalog = TableCatalog("users", "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
                                    columns, ("id",), (), ())
        up_catalog = TableCatalog("users", "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, status TEXT NOT NULL DEFAULT 'active')",
                                  columns + (ColumnSpec(2, "status", "TEXT", True, "'active'", 0),), ("id",), (), ())
        base_rows = ((TypedValue.integer(1), TypedValue.text("Ada")),
                     (TypedValue.integer(2), TypedValue.text("Lin")))
        up_rows = tuple(row + (TypedValue.text("active"),) for row in base_rows)
        for snapshot, catalog, rows in (
            (result.baseline_snapshot, base_catalog, base_rows),
            (result.down_snapshot, base_catalog, base_rows),
            (result.first_up_snapshot, up_catalog, up_rows),
            (result.second_up_snapshot, up_catalog, up_rows),
        ):
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.catalog.tables["users"], catalog)
            self.assertEqual(snapshot.tables["users"].rows, rows)
        self.assertTrue(result.restoration_diff.matched)
        self.assertTrue(result.reapply_diff.matched)

    def _fk_case(self, destructive=False):
        db = self.temp_dir / "teams.sqlite3"
        conn = sqlite3.connect(db)
        try:
            with conn:
                conn.execute("CREATE TABLE teams (id INTEGER PRIMARY KEY, code TEXT NOT NULL)")
                conn.execute("CREATE TABLE members (id INTEGER PRIMARY KEY, team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE, badge BLOB, notes TEXT)")
                conn.execute("CREATE INDEX idx_members_team ON members(team_id)")
                conn.execute("INSERT INTO teams VALUES (10,'core')")
                conn.execute("INSERT INTO members VALUES (1,10,X'CAFE',NULL)")
        finally:
            conn.close()
        down = ["DROP INDEX idx_teams_region", "ALTER TABLE teams DROP COLUMN region"]
        if destructive:
            down = ["DROP INDEX idx_teams_region",
                    "CREATE TABLE teams_restore (id INTEGER PRIMARY KEY, code TEXT NOT NULL)",
                    "INSERT INTO teams_restore SELECT id,code FROM teams", "DROP TABLE teams",
                    "CREATE TABLE teams (id INTEGER PRIMARY KEY, code TEXT NOT NULL)",
                    "INSERT INTO teams SELECT id,code FROM teams_restore", "DROP TABLE teams_restore"]
        manifest = parse_manifest_dict({
            "migration_id": "teams_region", "up_statements": [
                "ALTER TABLE teams ADD COLUMN region TEXT NOT NULL DEFAULT 'br'",
                "CREATE INDEX idx_teams_region ON teams(region)"],
            "down_statements": down, "post_up_invariants": [
                {"id": "region", "query": "SELECT region FROM teams WHERE id=10", "expected": {"type": "text", "value": "br"}},
                {"id": "badge", "query": "SELECT badge FROM members WHERE id=1", "expected": {"type": "blob", "value": "cafe"}}]})
        original = db.read_bytes()
        result = rehearse_migration(db, manifest, workspace_parent_dir=self.temp_dir)
        self.assertEqual(db.read_bytes(), original)
        return result

    def test_multi_table_with_foreign_keys_indexes_and_blobs(self):
        result = self._fk_case()
        self.assertTrue(result.success, result.failure)
        members = result.baseline_snapshot.catalog.tables["members"]
        self.assertEqual(members.foreign_keys, (ForeignKeySpec(0, 0, "teams", "team_id", "id", "NO ACTION", "CASCADE", "NONE"),))
        self.assertEqual(members.indexes, (IndexSpec("idx_members_team", False, "c", False, ("team_id",), "CREATE INDEX idx_members_team ON members(team_id)"),))
        expected = ((TypedValue.integer(1), TypedValue.integer(10), TypedValue.blob(b"\xca\xfe"), TypedValue.null()),)
        for snapshot in (result.baseline_snapshot, result.first_up_snapshot, result.down_snapshot, result.second_up_snapshot):
            self.assertEqual(snapshot.tables["members"].rows, expected)
        self.assertEqual(result.down_snapshot.tables["teams"].rows,
                         ((TypedValue.integer(10), TypedValue.text("core")),))

    def test_original_parent_rebuild_cascades_and_is_not_reversible(self):
        result = self._fk_case(destructive=True)
        self.assertFalse(result.success)
        self.assertEqual(result.failure.code.value, "RESTORATION_MISMATCH")
        self.assertTrue(result.restoration_diff.catalog_matched)
        self.assertEqual(result.down_snapshot.tables["members"].rows, ())
        self.assertEqual(result.restoration_diff.table_diffs["members"].missing_row_count, 1)
        self.assertTrue(result.source_preserved)

    def test_lossy_down_same_schema_objects_but_missing_lin(self):
        db = create_users_basic_baseline(self.temp_dir / "users.sqlite3")
        manifest = load_manifest_file(ROOT / "fixtures/manifests/users_add_status_lossy_down.json")
        result = rehearse_migration(db, manifest, workspace_parent_dir=self.temp_dir)
        self.assertEqual(result.failure.code.value, "RESTORATION_MISMATCH")
        self.assertTrue(result.restoration_diff.catalog_matched)
        self.assertFalse(result.restoration_diff.data_matched)
        self.assertEqual(result.down_snapshot.tables["users"].rows,
                         ((TypedValue.integer(1), TypedValue.text("Ada")),))
        self.assertEqual(result.restoration_diff.table_diffs["users"].missing_row_count, 1)
