"""Transaction escape, diagnostic, invariant, and step-budget attacks."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sqlite_migration_rehearsal.cli import main
from sqlite_migration_rehearsal.demo import (
    build_failing_up_after_update_manifest, build_reversible_users_status_manifest,
    build_sql_failure_after_update_manifest, create_users_basic_baseline, run_demo,
)
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.errors import ForeignKeyEnforcementError, RehearsalError
from sqlite_migration_rehearsal.manifest import parse_manifest_dict
from sqlite_migration_rehearsal.models import RehearsalLimits, TypedValue
from sqlite_migration_rehearsal.sql_guard import MigrationAuthorizer, ReadOnlyInvariantAuthorizer
from sqlite_migration_rehearsal.workspace import enable_and_verify_foreign_keys


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = create_users_basic_baseline(self.root / "users.sqlite3")
        self.original = self.db.read_bytes()

    def run_manifest(self, manifest, **kwargs):
        result = rehearse_migration(self.db, manifest, workspace_parent_dir=self.root, **kwargs)
        self.assertEqual(self.db.read_bytes(), self.original)
        self.assertTrue(result.workspace_cleaned)
        return result

    def test_sql_error_after_update_and_invariant_failure_both_restore_baseline(self):
        expected_rows = ((TypedValue.integer(1), TypedValue.text("Ada")),
                         (TypedValue.integer(2), TypedValue.text("Lin")))
        for manifest, code in ((build_sql_failure_after_update_manifest(), "SQL_EXECUTION_ERROR"),
                               (build_failing_up_after_update_manifest(), "INVARIANT_MISMATCH")):
            with self.subTest(code=code):
                result = self.run_manifest(manifest)
                self.assertEqual(result.failure.code.value, code)
                self.assertTrue(result.failure.transaction_rolled_back)
                self.assertTrue(result.transaction_rollback_verified)
                self.assertEqual(result.rolled_back_snapshot.tables["users"].rows, expected_rows)
                self.assertEqual(result.rolled_back_snapshot.catalog.tables["users"].create_sql,
                                 "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
                self.assertIsNone(result.down_snapshot)

    def test_transaction_control_and_multiple_statements_cannot_commit(self):
        for sql in ("/*x*/CoMmIt", "END TRANSACTION", "BEGIN", "SAVEPOINT x", "RELEASE x", "ROLLBACK",
                    "UPDATE users SET name='hidden'; COMMIT", "UPDATE OR ROLLBACK users SET id=1",
                    "ATTACH ':memory:' AS x", "DETACH x", "VACUUM INTO 'hidden'", "PRAGMA foreign_keys=OFF",
                    "CREATE VIRTUAL TABLE v USING fts5(x)", "CREATE VIEW v AS SELECT 1",
                    "CREATE TRIGGER t AFTER INSERT ON users BEGIN SELECT 1; END"):
            with self.subTest(sql=sql):
                # Raw dataclass construction must not bypass manifest validation.
                manifest = replace(build_reversible_users_status_manifest(), up_statements=(sql,))
                result = self.run_manifest(manifest)
                self.assertFalse(result.success)
                self.assertEqual(result.failure.code.value, "STATEMENT_REJECTED")

    def test_authorizers_independently_deny_operations_without_lexical_guards(self):
        conn = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t VALUES (1,'original')")
        for sql in ("COMMIT", "ROLLBACK", "SAVEPOINT x", "ATTACH ':memory:' AS x",
                    "PRAGMA foreign_keys=OFF", "CREATE VIEW v AS SELECT 1",
                    "CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END",
                    "CREATE VIRTUAL TABLE v USING fts5(x)", "SELECT \"load_extension\"('hidden')"):
            conn.execute("BEGIN")
            conn.execute("UPDATE t SET value='changed'")
            guard = MigrationAuthorizer()
            conn.set_authorizer(guard)
            try:
                with self.assertRaises(sqlite3.Error):
                    conn.execute(sql)
                self.assertIsNotNone(guard.denied_reason)
            finally:
                conn.set_authorizer(None)
                conn.execute("ROLLBACK")
            self.assertEqual(conn.execute("SELECT * FROM t").fetchall(), [(1, "original")])
        for sql in ("WITH x AS (SELECT 1) DELETE FROM t", "PRAGMA user_version=2",
                    "SELECT load_extension('hidden')", "SELECT * FROM pragma_table_info('t')"):
            guard = ReadOnlyInvariantAuthorizer()
            conn.set_authorizer(guard)
            try:
                with self.assertRaises(sqlite3.Error):
                    conn.execute(sql).fetchall()
            finally:
                conn.set_authorizer(None)

    def test_index_population_is_allowed_but_explicit_reindex_stays_denied(self):
        conn = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t VALUES (1,'kept')")
        conn.execute("BEGIN")
        guard = MigrationAuthorizer()
        conn.set_authorizer(guard)
        try:
            guard.begin_statement()
            conn.execute("CREATE INDEX idx_t_value ON t(value)")
            self.assertEqual(conn.execute("SELECT id FROM t INDEXED BY idx_t_value").fetchall(), [(1,)])
            # Authorization for population cannot be borrowed by a later command.
            guard.begin_statement()
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("REINDEX idx_t_value")
            self.assertEqual(guard.denied_reason, "reindex")
        finally:
            conn.set_authorizer(None)
            conn.execute("ROLLBACK")
        self.assertEqual(conn.execute("SELECT * FROM t").fetchall(), [(1, "kept")])
        self.assertEqual(conn.execute("PRAGMA index_list(t)").fetchall(), [])

    def test_main_alter_can_inspect_schema_without_granting_temp_access(self):
        conn = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, region TEXT)")
        conn.execute("CREATE TEMP TABLE private_temp(id INTEGER PRIMARY KEY, region TEXT)")
        guard = MigrationAuthorizer()
        conn.set_authorizer(guard)
        try:
            guard.begin_statement()
            conn.execute("ALTER TABLE main.t DROP COLUMN region")
            for sql in ("SELECT name FROM temp.sqlite_temp_master",
                        "SELECT * FROM temp.private_temp",
                        "ALTER TABLE temp.private_temp DROP COLUMN region"):
                guard.begin_statement()
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql).fetchall()
                self.assertEqual(guard.denied_reason, "non_main_database")
            # SQLite can reject direct schema writes before invoking the callback;
            # verify the callback policy independently for that privileged action.
            guard.begin_statement()
            self.assertEqual(guard(sqlite3.SQLITE_UPDATE, "sqlite_temp_master", "sql", "temp", None),
                             sqlite3.SQLITE_DENY)
        finally:
            conn.set_authorizer(None)
        self.assertEqual([r[1] for r in conn.execute("PRAGMA table_info(t)")], ["id"])
        self.assertEqual([r[1] for r in conn.execute("PRAGMA temp.table_info(private_temp)")], ["id", "region"])

    def test_foreign_keys_and_pragma_verification(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=OFF")
        with self.assertRaises(ForeignKeyEnforcementError):
            enable_and_verify_foreign_keys(conn, pragma_statement="PRAGMA foreign_key=ON")
        conn.execute("BEGIN")
        with self.assertRaises(ForeignKeyEnforcementError):
            enable_and_verify_foreign_keys(conn)
        conn.execute("ROLLBACK")
        enable_and_verify_foreign_keys(conn)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchall(), [(1,)])
        for deferred in (False, True):
            manifest = parse_manifest_dict({"migration_id": "fk", "up_statements": [
                "CREATE TABLE child(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id)" +
                (" DEFERRABLE INITIALLY DEFERRED" if deferred else "") + ")",
                "INSERT INTO child VALUES (1,999)"], "down_statements": ["DROP TABLE child"]})
            result = self.run_manifest(manifest)
            self.assertEqual(result.failure.code.value, "FOREIGN_KEY_VIOLATION")
            self.assertTrue(result.transaction_rollback_verified)

    def test_invariant_shape_types_case_and_read_only(self):
        for query, expected, code in (
            ("SELECT name FROM users", {"type": "text", "value": "Ada"}, "INVARIANT_SHAPE_ERROR"),
            ("SELECT 1,2", {"type": "integer", "value": 1}, "INVARIANT_SHAPE_ERROR"),
            ("SELECT 1 WHERE 0", {"type": "integer", "value": 1}, "INVARIANT_SHAPE_ERROR"),
            ("SELECT '2'", {"type": "integer", "value": 2}, "INVARIANT_MISMATCH"),
            ("SELECT CASE WHEN count(*)=2 THEN 1 ELSE 0 END FROM users", {"type": "integer", "value": 1}, None),
            ("SELECT zeroblob(1000000000)", {"type": "blob", "value": ""}, "SQL_EXECUTION_ERROR"),
        ):
            with self.subTest(query=query):
                manifest = parse_manifest_dict({"migration_id": "scalar", "up_statements": ["UPDATE users SET name=name"],
                    "down_statements": ["UPDATE users SET name=name"], "post_up_invariants": [
                        {"id": "scalar", "query": query, "expected": expected}]})
                result = self.run_manifest(manifest)
                if code:
                    self.assertEqual(result.failure.code.value, code)
                else:
                    self.assertTrue(result.success, result.failure)
        for query in ("WITH x AS (SELECT 1) DELETE FROM users RETURNING id", "PRAGMA foreign_keys", "SELECT load_extension('x')"):
            with self.assertRaises(RehearsalError):
                parse_manifest_dict({"migration_id": "readonly", "up_statements": ["DELETE FROM users"],
                    "down_statements": ["DELETE FROM users"], "post_up_invariants": [
                        {"id": "x", "query": query, "expected": {"type": "null", "value": None}}]})

    def test_vm_budget_covers_sql_and_catalog_inspection(self):
        manifest = parse_manifest_dict({"migration_id": "steps", "up_statements": [
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) UPDATE users SET name=(SELECT sum(x) FROM n)"],
            "down_statements": ["UPDATE users SET name=name"]})
        result = self.run_manifest(manifest, limits=RehearsalLimits(max_vm_steps=10000))
        self.assertEqual(result.failure.code.value, "STEP_BUDGET_EXCEEDED")
        self.assertTrue(result.transaction_rollback_verified)
        result = self.run_manifest(build_reversible_users_status_manifest(), limits=RehearsalLimits(max_vm_steps=1))
        self.assertEqual(result.failure.code.value, "STEP_BUDGET_EXCEEDED")
        self.assertEqual(result.failure.phase.value, "baseline_validation")
        inv = parse_manifest_dict({"migration_id": "invsteps", "up_statements": ["UPDATE users SET name=name"],
            "down_statements": ["UPDATE users SET name=name"], "post_up_invariants": [{"id": "expensive",
            "query": "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n",
            "expected": {"type": "integer", "value": 0}}]})
        result = self.run_manifest(inv, limits=RehearsalLimits(max_vm_steps=10000))
        self.assertEqual(result.failure.code.value, "STEP_BUDGET_EXCEEDED")
        self.assertEqual(result.failure.phase.value, "post_up_invariants")

    def test_diagnostics_do_not_echo_sql_values_or_paths(self):
        secret = "SYNTHETIC_PRIVATE_MARKER"
        manifest = {"migration_id": "sanitized", "up_statements": [
            "UPDATE users SET name='" + secret + "'", "INSERT INTO " + secret + " VALUES (1)"],
            "down_statements": ["DELETE FROM users"]}
        path = self.root / (secret + ".json")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(["run", "--baseline", str(self.db), "--manifest", str(path), "--json"])
        self.assertEqual(status, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(str(self.root), stdout.getvalue())
        self.assertNotIn("INSERT INTO", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["failure"]["code"], "SQL_EXECUTION_ERROR")
        path.write_text('{"' + secret + '":1}', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(["validate-manifest", "--manifest", str(path)]), 2)
        self.assertNotIn(secret, stderr.getvalue())

    def test_demo_and_exclusive_seed_creation(self):
        with self.assertRaises(FileExistsError):
            create_users_basic_baseline(self.db)
        self.assertEqual(self.db.read_bytes(), self.original)
        summary = run_demo()
        self.assertTrue(summary["all_expectations_met"])
        self.assertEqual(len(summary["scenarios"]), 4)
