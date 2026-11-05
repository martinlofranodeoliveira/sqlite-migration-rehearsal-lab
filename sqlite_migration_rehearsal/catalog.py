"""Bounded structured catalog and typed-row comparison, not SQL equivalence."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from sqlite_migration_rehearsal.errors import (
    BaselineValidationError, ErrorCode, ForeignKeyViolationError, RehearsalPhase,
    ResourceLimitError, SanitizedDiagnostic,
)
from sqlite_migration_rehearsal.models import (
    CatalogState, ColumnSpec, ForeignKeySpec, IndexSpec, RehearsalLimits,
    SnapshotState, StateDiff, TableCatalog, TableDiff, TableState, TypedValue,
    MAX_SCHEMA_OBJECTS, MAX_COLUMNS, MAX_INDEXES, MAX_IDENTIFIER_BYTES,
    MAX_CATALOG_BYTES, MAX_STATE_BYTES,
)
from sqlite_migration_rehearsal.sql_guard import tokenize_sql, vm_budget


def _quote_ident(value):
    return '"' + value.replace('"', '""') + '"'


def _fail(code, phase, message):
    raise BaselineValidationError(SanitizedDiagnostic(code, phase, message))


def _limit(phase):
    raise ResourceLimitError(SanitizedDiagnostic(
        ErrorCode.RESOURCE_LIMIT_EXCEEDED, phase, "Snapshot resource limit exceeded."
    ))


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _identifier(value, phase):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES or "\x00" in value:
        _limit(phase)
    return value


def _bounded(cursor, maximum, phase):
    try:
        rows = cursor.fetchmany(maximum + 1)
        if len(rows) > maximum:
            _limit(phase)
        return rows
    finally:
        cursor.close()


def run_integrity_and_fk_checks(conn, *, phase):
    # Only existence of an FK violation is needed; do not collect its row contents.
    cursor = conn.execute("PRAGMA foreign_key_check")
    try:
        violated = cursor.fetchone() is not None
    finally:
        cursor.close()
    if violated:
        raise ForeignKeyViolationError(SanitizedDiagnostic(
            ErrorCode.FOREIGN_KEY_VIOLATION, phase, "Foreign-key check found a violation."
        ))
    rows = _bounded(conn.execute("PRAGMA integrity_check(1)"), 1, phase)
    if rows != [("ok",)]:
        _fail(ErrorCode.INTEGRITY_CHECK_FAILED, phase, "Database failed the integrity check.")


def inspect_snapshot(conn: sqlite3.Connection, *, limits: RehearsalLimits,
                     phase: RehearsalPhase) -> SnapshotState:
    with vm_budget(conn, limits, phase):
        return _inspect(conn, limits, phase)


def _inspect(conn, limits, phase):
    schema = _bounded(conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    ), MAX_SCHEMA_OBJECTS, phase)
    table_sql = {}
    index_sql = {}
    for kind, name, owner, sql in schema:
        _identifier(name, phase)
        _identifier(owner, phase)
        if kind == "index" and name.startswith("sqlite_autoindex_") and sql is None:
            continue
        if kind not in ("table", "index") or name.startswith("sqlite_") or not isinstance(sql, str):
            _fail(ErrorCode.UNSUPPORTED_SCHEMA_OBJECT, phase, "Unsupported catalog object.")
        if len(sql.encode("utf-8")) > limits.max_statement_bytes:
            _limit(phase)
        if kind == "table":
            words = [t.upper for t in tokenize_sql(sql, phase=phase) if t.kind == "WORD"]
            if "VIRTUAL" in words or "GENERATED" in words or any(
                words[i:i + 2] == ["WITHOUT", "ROWID"] for i in range(len(words) - 1)
            ):
                _fail(ErrorCode.UNSUPPORTED_SCHEMA_OBJECT, phase, "Unsupported table definition.")
            table_sql[name] = sql
        else:
            index_sql[name] = sql
    if len(table_sql) > limits.max_tables or len(index_sql) > MAX_INDEXES:
        _limit(phase)

    catalogs = {}
    tables = {}
    total_rows = 0
    encoded_rows = 0
    index_count = 0
    for name in sorted(table_sql):
        quoted = _quote_ident(name)
        info = _bounded(conn.execute(f"PRAGMA table_xinfo({quoted})"), MAX_COLUMNS, phase)
        if not info or any(row[6] != 0 for row in info):
            _fail(ErrorCode.UNSUPPORTED_SCHEMA_OBJECT, phase, "Hidden or generated columns are unsupported.")
        columns = tuple(ColumnSpec(int(cid), _identifier(col, phase), str(decl or ""),
                                   bool(nn), default, int(pk))
                        for cid, col, decl, nn, default, pk, hidden in info)
        pk_pairs = sorted((c.pk_index, c.name, i) for i, c in enumerate(columns) if c.pk_index)
        if not pk_pairs:
            _fail(ErrorCode.UNSUPPORTED_TABLE_KEY, phase, "Every table requires an explicit primary key.")
        pk_columns = tuple(p[1] for p in pk_pairs)
        pk_indices = tuple(p[2] for p in pk_pairs)
        fk_rows = _bounded(conn.execute(f"PRAGMA foreign_key_list({quoted})"), MAX_SCHEMA_OBJECTS, phase)
        fks = []
        for fk_id, seq, target, source, dest, update, delete, match in fk_rows:
            for identifier in (target, source, dest or ""):
                _identifier(identifier, phase)
            fks.append(ForeignKeySpec(fk_id, seq, target, source, dest or "", update, delete, match))
        fks.sort(key=lambda f: (f.fk_id, f.seq))
        indexes = []
        idx_rows = _bounded(conn.execute(f"PRAGMA index_list({quoted})"), MAX_INDEXES, phase)
        index_count += len(idx_rows)
        if index_count > MAX_INDEXES:
            _limit(phase)
        for seq, idx_name, unique, origin, partial in idx_rows:
            _identifier(idx_name, phase)
            xinfo = _bounded(conn.execute(f"PRAGMA index_xinfo({_quote_ident(idx_name)})"), MAX_COLUMNS + 1, phase)
            idx_columns = []
            for seqno, cid, column, descending, collation, key in xinfo:
                if not key:
                    continue
                if cid < 0 or column is None or collation not in ("BINARY", "NOCASE", "RTRIM"):
                    _fail(ErrorCode.UNSUPPORTED_SCHEMA_OBJECT, phase, "Expression or custom-collation indexes are unsupported.")
                idx_columns.append(_identifier(column, phase))
            indexes.append(IndexSpec(idx_name, bool(unique), origin, bool(partial),
                                     tuple(idx_columns), index_sql.get(idx_name)))
        indexes.sort(key=lambda i: i.name)
        catalogs[name] = TableCatalog(name, table_sql[name], columns, pk_columns, tuple(fks), tuple(indexes))
        table, total_rows = _inspect_table_rows(
            conn, name, column_names=tuple(c.name for c in columns), pk_columns=pk_columns,
            pk_indices=pk_indices, current_total_rows=total_rows, limits=limits, phase=phase
        )
        for row in table.rows:
            encoded_rows += len(_json([v.to_dict() for v in row]))
            if encoded_rows > MAX_STATE_BYTES:
                _limit(phase)
        tables[name] = table
    catalog = CatalogState(catalogs)
    if len(_json(catalog.to_dict())) > MAX_CATALOG_BYTES:
        _limit(phase)
    run_integrity_and_fk_checks(conn, phase=phase)
    payload = _json({"catalog": catalog.to_dict(),
                     "tables": {n: tables[n].to_dict() for n in sorted(tables)}})
    if len(payload) > MAX_STATE_BYTES:
        _limit(phase)
    return SnapshotState(catalog, tables, total_rows, hashlib.sha256(payload).hexdigest())


def _inspect_table_rows(conn, table_name, *, column_names, pk_columns, pk_indices,
                        current_total_rows, limits, phase):
    cursor = conn.execute("SELECT " + ",".join(_quote_ident(c) for c in column_names)
                          + " FROM " + _quote_ident(table_name))
    seen = set()
    keyed = []
    byte_count = 0
    total = current_total_rows
    try:
        for raw in cursor:
            total += 1
            if total > limits.max_rows:
                _limit(phase)
            row = tuple(TypedValue.from_sql_value(v, max_cell_bytes=limits.max_cell_bytes, phase=phase)
                        for v in raw)
            byte_count += len(_json([v.to_dict() for v in row]))
            if byte_count > MAX_STATE_BYTES:
                _limit(phase)
            key = tuple(row[i] for i in pk_indices)
            if any(v.storage_type == "null" for v in key) or key in seen:
                _fail(ErrorCode.INVALID_PRIMARY_KEY_DATA, phase, "Actual primary-key values must be nonnull and unique.")
            seen.add(key)
            keyed.append((tuple(v.sort_key() for v in key), row))
    finally:
        cursor.close()
    keyed.sort(key=lambda pair: pair[0])
    return TableState(table_name, column_names, pk_columns, tuple(r for k, r in keyed)), total


def compare_snapshots(expected: SnapshotState, actual: SnapshotState) -> StateDiff:
    exp_names, act_names = set(expected.catalog.tables), set(actual.catalog.tables)
    missing, extra = tuple(sorted(exp_names - act_names)), tuple(sorted(act_names - exp_names))
    catalog_match = data_match = not missing and not extra
    diffs = {}
    for name in sorted(exp_names & act_names):
        ec, ac = expected.catalog.tables[name], actual.catalog.tables[name]
        er, ar = expected.tables[name], actual.tables[name]
        flags = (ec.create_sql == ac.create_sql, ec.columns == ac.columns,
                 ec.primary_key == ac.primary_key, ec.foreign_keys == ac.foreign_keys,
                 ec.indexes == ac.indexes)
        rows_match = er.columns == ar.columns and er.rows == ar.rows
        if er.columns == ar.columns and er.primary_key == ar.primary_key:
            indices = tuple(er.columns.index(c) for c in er.primary_key)
            e = {tuple(row[i] for i in indices): row for row in er.rows}
            a = {tuple(row[i] for i in indices): row for row in ar.rows}
            removed, added = len(e.keys() - a.keys()), len(a.keys() - e.keys())
            modified = sum(e[k] != a[k] for k in e.keys() & a.keys())
        else:
            removed, added = max(0, len(er.rows) - len(ar.rows)), max(0, len(ar.rows) - len(er.rows))
            modified = min(len(er.rows), len(ar.rows)) if not rows_match else 0
        diffs[name] = TableDiff(name, all(flags) and rows_match, *flags, rows_match,
                                len(er.rows), len(ar.rows), removed, added, modified)
        catalog_match = catalog_match and all(flags)
        data_match = data_match and rows_match
    return StateDiff(catalog_match and data_match, catalog_match, data_match, missing, extra, diffs)
