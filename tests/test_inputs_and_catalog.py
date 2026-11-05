"""Malformed inputs, conservative catalog equality and actual key/value checks."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlite_migration_rehearsal.catalog import _inspect_table_rows, compare_snapshots, inspect_snapshot
from sqlite_migration_rehearsal.demo import build_reversible_users_status_manifest
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.errors import RehearsalError, RehearsalPhase
from sqlite_migration_rehearsal.manifest import load_manifest_file, parse_manifest_bytes, parse_manifest_dict
from sqlite_migration_rehearsal.models import RehearsalLimits, TypedValue

PHASE = RehearsalPhase.BASELINE_VALIDATION


def payload():
    return {"migration_id": "test", "up_statements": ["UPDATE t SET id=id"],
            "down_statements": ["UPDATE t SET id=id"]}


class InputTests(unittest.TestCase):
    def test_manifest_adversaries_and_exact_boundaries(self):
        cases = [b"\xff", b'{"migration_id":"a","migration_id":"b"}', b'{"x":NaN}',
                 b"[" * 13 + b"0" + b"]" * 13, b"{", b"null"]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(RehearsalError):
                parse_manifest_bytes(raw)
        for mutation in (
            {"extra_private_value": 1}, {"schema_version": "private_value"},
            {"migration_id": "id\n"}, {"up_statements": []},
            {"up_statements": ["UPDATE t SET id=id"] * 65},
            {"up_statements": ["UPDATE t SET id=id " + " " * 16384]},
            {"description": "\ud800"},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(RehearsalError):
                parse_manifest_dict({**payload(), **mutation})
        raw = json.dumps(payload()).encode()
        parse_manifest_bytes(raw, limits=RehearsalLimits(max_manifest_bytes=len(raw)))
        with self.assertRaises(RehearsalError):
            parse_manifest_bytes(raw, limits=RehearsalLimits(max_manifest_bytes=len(raw) - 1))
        parse_manifest_dict({**payload(), "up_statements": ["UPDATE t SET id=id"] * 64})
        sql = "UPDATE t SET id=id"
        parse_manifest_dict({**payload(), "up_statements": [sql + " " * (16384 - len(sql))]})
        cyclic = payload()
        cyclic["cycle"] = cyclic
        with self.assertRaises(RehearsalError):
            parse_manifest_dict(cyclic)

    def test_expected_scalars_and_strict_limits(self):
        for expected in ({"type": "integer", "value": True}, {"type": "integer", "value": 2**63},
                         {"type": [], "value": None}, {"type": "real", "value": 1},
                         {"type": "text", "value": "\ud800"}, {"type": "blob", "value": "xx"}):
            with self.subTest(expected=expected), self.assertRaises(RehearsalError):
                parse_manifest_dict({**payload(), "post_up_invariants": [
                    {"id": "x", "query": "SELECT 1", "expected": expected}]})
        self.assertEqual(TypedValue.blob(b"\x00\xff").value, "00ff")
        for value in (True, 1.5, "100", 0, 500001):
            with self.assertRaises(ValueError):
                RehearsalLimits(max_vm_steps=value)

    def test_paths_are_sanitized_and_never_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for bad in (None, 12, "\x00", root, root / "missing"):
                result = rehearse_migration(bad, build_reversible_users_status_manifest())
                self.assertEqual(result.failure.code.value, "INVALID_BASELINE")
                with self.assertRaises(RehearsalError):
                    load_manifest_file(bad)
            oversized = root / "oversized.sqlite3"
            with oversized.open("wb") as stream:
                stream.truncate(16777217)
            result = rehearse_migration(oversized, build_reversible_users_status_manifest())
            self.assertEqual(result.failure.code.value, "RESOURCE_LIMIT_EXCEEDED")
            link = root / "link"
            link.symlink_to(oversized)
            result = rehearse_migration(link, build_reversible_users_status_manifest())
            self.assertEqual(result.failure.code.value, "INVALID_BASELINE")


class CatalogTests(unittest.TestCase):
    def snapshot(self, ddl, rows=(), *, limits=None):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute(ddl)
        for sql in rows:
            conn.execute(sql)
        return inspect_snapshot(conn, limits=limits or RehearsalLimits(), phase=PHASE)

    def test_unsupported_schema_and_real_values(self):
        for ddl, rows, code in (
            ("CREATE TABLE t(x TEXT)", (), "UNSUPPORTED_TABLE_KEY"),
            ("CREATE TABLE t(id TEXT PRIMARY KEY)", ("INSERT INTO t VALUES(NULL)",), "INVALID_PRIMARY_KEY_DATA"),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY,x REAL)", ("INSERT INTO t VALUES(1,1.5)",), "UNSUPPORTED_VALUE_TYPE"),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY,x AS (id+1))", (), "UNSUPPORTED_SCHEMA_OBJECT"),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY) WITHOUT ROWID", (), "UNSUPPORTED_SCHEMA_OBJECT"),
            ("CREATE VIEW t AS SELECT 1", (), "UNSUPPORTED_SCHEMA_OBJECT"),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY)", ("CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END",), "UNSUPPORTED_SCHEMA_OBJECT"),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY,x TEXT)", ("CREATE INDEX ix ON t(lower(x))",), "UNSUPPORTED_SCHEMA_OBJECT"),
        ):
            with self.subTest(ddl=ddl), self.assertRaises(RehearsalError) as caught:
                self.snapshot(ddl, rows)
            self.assertEqual(caught.exception.diagnostic.code.value, code)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        try:
            conn.execute("CREATE VIRTUAL TABLE v USING fts5(x)")
        except sqlite3.OperationalError:
            self.skipTest("SQLite build does not include FTS5")
        with self.assertRaises(RehearsalError) as caught:
            inspect_snapshot(conn, limits=RehearsalLimits(), phase=PHASE)
        self.assertEqual(caught.exception.diagnostic.code.value, "UNSUPPORTED_SCHEMA_OBJECT")

    def test_duplicate_actual_keys_checked_even_if_declaration_cannot_be_trusted(self):
        # Exercise row validation over real duplicate SQLite rows independently of DDL enforcement.
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t(k TEXT,v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?,?)", [("same", "a"), ("same", "b")])
        with self.assertRaises(RehearsalError) as caught:
            _inspect_table_rows(conn, "t", column_names=("k", "v"), pk_columns=("k",),
                                pk_indices=(0,), current_total_rows=0, limits=RehearsalLimits(), phase=PHASE)
        self.assertEqual(caught.exception.diagnostic.code.value, "INVALID_PRIMARY_KEY_DATA")

    def test_typed_order_composite_keys_and_exact_sql(self):
        state = self.snapshot('CREATE TABLE "t" (a TEXT, b INTEGER, v BLOB, PRIMARY KEY(a,b))', (
            "INSERT INTO t VALUES ('z',2,X'00')", "INSERT INTO t VALUES ('a',9,NULL)",
            "INSERT INTO t VALUES ('a',1,X'FF')"))
        self.assertEqual(state.tables["t"].rows, (
            (TypedValue.text("a"), TypedValue.integer(1), TypedValue.blob(b"\xff")),
            (TypedValue.text("a"), TypedValue.integer(9), TypedValue.null()),
            (TypedValue.text("z"), TypedValue.integer(2), TypedValue.blob(b"\x00"))))
        first = self.snapshot("CREATE TABLE t(id INTEGER PRIMARY KEY,x TEXT)")
        second = self.snapshot("CREATE TABLE t ( id INTEGER PRIMARY KEY, x TEXT )")
        diff = compare_snapshots(first, second)
        self.assertFalse(diff.matched)
        self.assertTrue(diff.data_matched)
        self.assertFalse(diff.table_diffs["t"].create_sql_matched)

    def test_schema_rows_cells_and_tables_are_bounded(self):
        for ddl, rows, limits in (
            ("CREATE TABLE t(id INTEGER PRIMARY KEY)", ("INSERT INTO t VALUES(1)", "INSERT INTO t VALUES(2)"), RehearsalLimits(max_rows=1)),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY,v TEXT)", ("INSERT INTO t VALUES(1,'" + "x" * 65 + "')",), RehearsalLimits(max_cell_bytes=64)),
            ("CREATE TABLE t(id INTEGER PRIMARY KEY)", ("CREATE TABLE u(id INTEGER PRIMARY KEY)",), RehearsalLimits(max_tables=1)),
            ("CREATE TABLE t(" + ",".join(["id INTEGER PRIMARY KEY"] + ["x" + str(i) for i in range(64)]) + ")", (), RehearsalLimits()),
        ):
            with self.assertRaises(RehearsalError) as caught:
                self.snapshot(ddl, rows, limits=limits)
            self.assertEqual(caught.exception.diagnostic.code.value, "RESOURCE_LIMIT_EXCEEDED")

    def test_foreign_key_check_and_integrity_check_are_distinct(self):
        with self.assertRaises(RehearsalError) as caught:
            self.snapshot("CREATE TABLE t(id INTEGER PRIMARY KEY,parent INTEGER REFERENCES t(id))",
                          ("INSERT INTO t VALUES(1,9)",))
        self.assertEqual(caught.exception.diagnostic.code.value, "FOREIGN_KEY_VIOLATION")
        with self.assertRaises(RehearsalError) as caught:
            self.snapshot("CREATE TABLE t(id INTEGER PRIMARY KEY,x INTEGER CHECK(x>0))",
                          ("PRAGMA ignore_check_constraints=ON", "INSERT INTO t VALUES(1,-1)",
                           "PRAGMA ignore_check_constraints=OFF"))
        self.assertEqual(caught.exception.diagnostic.code.value, "INTEGRITY_CHECK_FAILED")
