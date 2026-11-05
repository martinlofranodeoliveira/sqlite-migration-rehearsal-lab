"""Conservative SQL guards, authorizers and bounded VM execution; not a sandbox."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from sqlite_migration_rehearsal.errors import (
    ErrorCode, InvariantViolationError, RehearsalPhase, ResourceLimitError,
    SanitizedDiagnostic, StatementGuardError, StepBudgetExceededError,
    classify_sqlite_error,
)

# No application-defined functions or extension loading are registered.
SAFE_FUNCTIONS = frozenset("abs changes char coalesce concat concat_ws count format glob hex if ifnull iif instr last_insert_rowid length like likelihood likely lower ltrim max min nullif octet_length printf quote random randomblob replace round rtrim sign sqlite_version substr substring sum total total_changes trim typeof unicode unlikely unhex upper zeroblob".split())
# SQLite uses these string-processing helpers while rewriting ordinary DDL.
DDL_FUNCTIONS = frozenset("sqlite_rename_table sqlite_rename_column sqlite_rename_test sqlite_rename_quotefix sqlite_drop_column".split())
TX = frozenset("BEGIN COMMIT END ROLLBACK SAVEPOINT RELEASE".split())
UNSAFE = frozenset("ATTACH DETACH VACUUM PRAGMA REINDEX ANALYZE LOAD_EXTENSION".split())
STARTERS = frozenset("CREATE ALTER DROP INSERT UPDATE DELETE REPLACE WITH".split())


@dataclass(frozen=True)
class SqlToken:
    kind: str
    upper: str


def _reject(phase, statement_index=None, invariant_id=None):
    raise StatementGuardError(SanitizedDiagnostic(
        ErrorCode.STATEMENT_REJECTED, phase, "SQL entry violates the single-statement policy.",
        statement_index=statement_index, invariant_id=invariant_id
    ))


def tokenize_sql(sql: str, *, phase: RehearsalPhase,
                 statement_index: int | None = None,
                 invariant_id: str | None = None) -> list[SqlToken]:
    tokens = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                _reject(phase, statement_index, invariant_id)
            i = end + 2
        elif ch in "'\"`[":
            close = "]" if ch == "[" else ch
            start = i + 1
            i += 1
            while i < len(sql):
                if sql[i] == close:
                    if close != "]" and i + 1 < len(sql) and sql[i + 1] == close:
                        i += 2
                        continue
                    body = sql[start:i].replace(close * 2, close)
                    tokens.append(SqlToken("STRING" if ch == "'" else "IDENT", body.upper() if ch != "'" else ""))
                    i += 1
                    break
                i += 1
            else:
                _reject(phase, statement_index, invariant_id)
        elif ch == ";":
            tokens.append(SqlToken("SEMICOLON", ";"))
            i += 1
        elif ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            tokens.append(SqlToken("WORD", sql[start:i].upper()))
        else:
            tokens.append(SqlToken("SYMBOL", ch))
            i += 1
    return tokens


def _tokens(sql, limit, phase, statement_index=None, invariant_id=None):
    if not isinstance(sql, str) or "\x00" in sql:
        _reject(phase, statement_index, invariant_id)
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeError:
        _reject(phase, statement_index, invariant_id)
    if size > limit:
        raise ResourceLimitError(SanitizedDiagnostic(
            ErrorCode.RESOURCE_LIMIT_EXCEEDED, phase, "SQL byte limit exceeded.",
            statement_index=statement_index, invariant_id=invariant_id
        ))
    tokens = tokenize_sql(sql, phase=phase, statement_index=statement_index, invariant_id=invariant_id)
    if tokens and tokens[-1].kind == "SEMICOLON":
        tokens.pop()
    if not tokens or any(t.kind == "SEMICOLON" for t in tokens) or tokens[0].kind != "WORD":
        _reject(phase, statement_index, invariant_id)
    return tokens


def validate_migration_statement(sql: str, *, max_statement_bytes: int,
                                 phase: RehearsalPhase, statement_index: int) -> None:
    tokens = _tokens(sql, max_statement_bytes, phase, statement_index)
    words = [t.upper for t in tokens if t.kind == "WORD"]
    identifiers = {t.upper for t in tokens if t.kind in ("WORD", "IDENT")}
    # END inside CASE is not transaction control. ON CONFLICT ROLLBACK is.
    if (tokens[0].upper not in STARTERS or tokens[0].upper in TX
            or set(words) & (UNSAFE | {"ROLLBACK"}) or "LOAD_EXTENSION" in identifiers):
        _reject(phase, statement_index)
    if tokens[0].upper == "CREATE" and set(words[1:4]) & {"TEMP", "TEMPORARY", "VIRTUAL", "VIEW", "TRIGGER"}:
        _reject(phase, statement_index)
    if tokens[0].upper in {"CREATE", "ALTER"}:
        if "GENERATED" in words or any(words[i:i + 2] == ["WITHOUT", "ROWID"] for i in range(len(words) - 1)):
            _reject(phase, statement_index)


def validate_invariant_query(query: str, *, max_statement_bytes: int,
                             phase: RehearsalPhase, invariant_id: str) -> None:
    tokens = _tokens(query, max_statement_bytes, phase, invariant_id=invariant_id)
    words = {t.upper for t in tokens if t.kind == "WORD"}
    identifiers = {t.upper for t in tokens if t.kind in ("WORD", "IDENT")}
    forbidden = UNSAFE | {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}
    if tokens[0].upper not in {"SELECT", "WITH"} or words & forbidden or "LOAD_EXTENSION" in identifiers:
        raise InvariantViolationError(SanitizedDiagnostic(
            ErrorCode.INVARIANT_INVALID_QUERY, phase, "Invariant must be a permitted read-only SELECT.",
            invariant_id=invariant_id
        ))


class MigrationAuthorizer:
    _DENIED_ACTIONS = {getattr(sqlite3, "SQLITE_" + name): name.lower() for name in (
        "TRANSACTION", "SAVEPOINT", "ATTACH", "DETACH", "PRAGMA", "CREATE_VTABLE", "DROP_VTABLE",
        "CREATE_VIEW", "CREATE_TEMP_VIEW", "DROP_VIEW", "DROP_TEMP_VIEW", "CREATE_TRIGGER",
        "CREATE_TEMP_TRIGGER", "DROP_TRIGGER", "DROP_TEMP_TRIGGER", "CREATE_TEMP_TABLE",
        "CREATE_TEMP_INDEX", "DROP_TEMP_TABLE", "DROP_TEMP_INDEX", "ANALYZE", "REINDEX"
    )}

    def __init__(self) -> None:
        self.denied_reason: str | None = None
        self._created_indexes: set[tuple[str | None, str | None]] = set()
        self._altering_main = False

    def begin_statement(self) -> None:
        self.denied_reason = None
        self._created_indexes.clear()
        self._altering_main = False

    def __call__(self, action_code, arg1, arg2, db_name, trigger_or_view):
        reason = None
        if action_code == sqlite3.SQLITE_ALTER_TABLE:
            # ALTER_TABLE identifies the database in arg1. Some SQLite versions
            # use the generic database slot for the affected column instead.
            if (arg1 or "").lower() != "main":
                reason = "non_main_database"
            else:
                self._altering_main = True
        elif (self._altering_main and db_name == "temp"
              and arg1 in ("sqlite_temp_master", "sqlite_temp_schema")
              and (action_code == sqlite3.SQLITE_READ
                   or (action_code == sqlite3.SQLITE_UPDATE and arg2 == "sql"))):
            # ALTER inspects/rewrites dependent temporary schema definitions.
            # This cannot authorize a later statement or temporary table data.
            pass
        elif db_name not in (None, "", "main"):
            reason = "non_main_database"
        elif action_code == sqlite3.SQLITE_CREATE_INDEX:
            self._created_indexes.add((db_name, arg1))
        elif action_code == sqlite3.SQLITE_REINDEX and (db_name, arg1) in self._created_indexes:
            # SQLite authorizes index population with REINDEX during CREATE INDEX.
            # Consume only the matching authorization from this same statement.
            self._created_indexes.remove((db_name, arg1))
        elif action_code in self._DENIED_ACTIONS:
            reason = self._DENIED_ACTIONS[action_code]
        elif action_code == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() not in SAFE_FUNCTIONS | DDL_FUNCTIONS:
            reason = "unapproved_function"
        if reason:
            self.denied_reason = reason
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


class ReadOnlyInvariantAuthorizer:
    _ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

    def __init__(self) -> None:
        self.denied_reason: str | None = None

    def __call__(self, action_code, arg1, arg2, db_name, trigger_or_view):
        reason = None
        if db_name not in (None, "", "main"):
            reason = "non_main_database"
        elif action_code not in self._ALLOWED_ACTIONS:
            reason = "non_readonly_operation"
        elif action_code == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() not in SAFE_FUNCTIONS:
            reason = "unapproved_function"
        if reason:
            self.denied_reason = reason
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


class ProgressStepTracker:
    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.steps = 0
        self.exceeded = False

    def __call__(self) -> int:
        self.steps += 1
        self.exceeded = self.steps > self.max_steps
        return int(self.exceeded)


@contextmanager
def vm_budget(conn, limits, phase):
    """For engine inspection only; do not nest with a migration authorizer."""
    tracker = ProgressStepTracker(limits.max_vm_steps)
    conn.set_progress_handler(tracker, 1)
    try:
        yield tracker
    except sqlite3.Error as exc:
        if tracker.exceeded:
            raise StepBudgetExceededError(SanitizedDiagnostic(
                ErrorCode.STEP_BUDGET_EXCEEDED, phase, "SQLite inspection VM budget exceeded."
            )) from None
        raise classify_sqlite_error(exc, phase=phase) from None
    finally:
        conn.set_progress_handler(None, 0)
