"""Bounded regular-file reads, read-only SQLite backup and explicit FK verification."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from pathlib import Path

from sqlite_migration_rehearsal.errors import (
    BaselineValidationError, ErrorCode, ForeignKeyEnforcementError,
    RehearsalPhase, ResourceLimitError, SanitizedDiagnostic,
)
from sqlite_migration_rehearsal.runtime import require_qualified_sqlite
from sqlite_migration_rehearsal.models import (
    MAX_BASELINE_BYTES, MAX_COLUMNS, MAX_STATEMENT_BYTES, RehearsalLimits,
)


def _invalid(message="Cannot access a regular baseline file."):
    return BaselineValidationError(SanitizedDiagnostic(
        ErrorCode.INVALID_BASELINE, RehearsalPhase.SOURCE_PREPARATION, message
    ))


def _limit():
    return ResourceLimitError(SanitizedDiagnostic(
        ErrorCode.RESOURCE_LIMIT_EXCEEDED, RehearsalPhase.SOURCE_PREPARATION,
        "Source, backup, or backup iteration limit exceeded."
    ))


def regular_reader(path):
    """Open a finite trusted regular file; refuse final-component links and devices.

    O_NONBLOCK avoids a replaced FIFO blocking on open on supporting platforms.
    Parent directories and concurrent filesystem mutation remain caller concerns.
    """
    fd = None
    try:
        candidate = Path(path)
        if not stat.S_ISREG(candidate.lstat().st_mode):
            raise _invalid()
        fd = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _invalid()
        stream = os.fdopen(fd, "rb")
        fd = None
        return stream
    except (OSError, TypeError, ValueError):
        raise _invalid() from None
    finally:
        if fd is not None:
            os.close(fd)


def compute_file_sha256(path, *, max_bytes: int = MAX_BASELINE_BYTES) -> str:
    """Hash at most max_bytes; detect a growing file with a one-byte probe."""
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BASELINE_BYTES:
        raise _limit()
    digest = hashlib.sha256()
    try:
        with regular_reader(path) as stream:
            remaining = max_bytes
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise _limit()
        return digest.hexdigest()
    except OSError:
        raise _invalid() from None


def _check_sidecars(path: Path, limits: RehearsalLimits) -> None:
    # Bound existing WAL/rollback-journal bytes as well as the main file. Never delete them.
    try:
        size = path.stat().st_size
        for suffix in ("-wal", "-journal", "-shm"):
            sidecar = Path(str(path) + suffix)
            try:
                info = sidecar.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise _invalid("SQLite sidecars must be regular files.")
            size += info.st_size
        if size > limits.max_baseline_bytes:
            raise _limit()
    except OSError:
        raise _invalid() from None


def validate_source_database_file(source_path, *, limits: RehearsalLimits):
    try:
        with regular_reader(source_path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_size > limits.max_baseline_bytes:
                raise _limit()
            if info.st_size < 100 or stream.read(16) != b"SQLite format 3\x00":
                raise _invalid("Baseline does not have a SQLite 3 header.")
        resolved = Path(source_path).resolve(strict=True)
        _check_sidecars(resolved, limits)
        return resolved, info.st_size, compute_file_sha256(resolved, max_bytes=limits.max_baseline_bytes)
    except (OSError, TypeError, ValueError):
        raise _invalid() from None


def open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                               isolation_level=None, timeout=0.05, cached_statements=0)
        require_qualified_sqlite(conn)
        conn.execute("PRAGMA query_only = ON")
        return conn
    except (sqlite3.Error, OSError, ValueError, RuntimeError):
        if conn is not None:
            conn.close()
        raise _invalid("Cannot open baseline read-only with actual linked SQLite 3.51.3 or newer.") from None


def configure_working_connection(conn, limits):
    # SQLITE_LIMIT_LENGTH also bounds a single SQLite record, not only a cell.
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 262144)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_STATEMENT_BYTES)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS)
    conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 64)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 32)
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
    conn.execute("PRAGMA trusted_schema = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = limits.max_baseline_bytes // page_size
    actual = conn.execute(f"PRAGMA max_page_count = {pages}").fetchone()
    if not actual or actual[0] > pages:
        raise _limit()


def backup_source_to_workspace(source_path: Path, destination_path: Path, *, limits: RehearsalLimits):
    source = None
    work = None
    succeeded = False
    try:
        # Exclusive creation: even callers of this lower-level helper cannot overwrite a file.
        fd = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        _check_sidecars(source_path, limits)
        source = open_readonly_sqlite(source_path)
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        page_count = source.execute("PRAGMA page_count").fetchone()[0]
        if page_size not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
            raise _invalid("Unsupported SQLite page size.")
        max_pages = limits.max_baseline_bytes // page_size
        if page_count > max_pages:
            raise _limit()
        work = sqlite3.connect(str(destination_path), isolation_level=None,
                               timeout=0.05, cached_statements=0)
        calls = 0
        busy = 0

        def progress(status, remaining, total):
            nonlocal calls, busy
            calls += 1
            busy += int(status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
            # Connection.backup does not invoke the VM progress handler. This is its own budget.
            if calls > max_pages + 8 or busy > 8 or total > max_pages or remaining > max_pages:
                raise _limit()
            _check_sidecars(source_path, limits)
            if destination_path.stat().st_size > limits.max_baseline_bytes:
                raise _limit()

        # One page per callback limits pre-callback copy overshoot to one page (at most 64 KiB).
        source.backup(work, pages=1, progress=progress, sleep=0.0)
        if destination_path.stat().st_size > limits.max_baseline_bytes:
            raise _limit()
        configure_working_connection(work, limits)
        succeeded = True
        return work
    except (sqlite3.Error, OSError, TypeError, ValueError):
        raise _invalid("Cannot create a bounded consistent SQLite backup.") from None
    finally:
        if source is not None:
            source.close()
        if work is not None and not succeeded:
            work.close()


def enable_and_verify_foreign_keys(conn, *, pragma_statement="PRAGMA foreign_keys = ON",
                                   phase=RehearsalPhase.BASELINE_VALIDATION):
    # The argument is retained for testing verification of a mistyped required PRAGMA.
    if conn.in_transaction:
        raise ForeignKeyEnforcementError(SanitizedDiagnostic(
            ErrorCode.FOREIGN_KEY_ENFORCEMENT_FAILED, phase,
            "Foreign keys must be enabled before beginning a transaction."
        ))
    try:
        conn.execute(pragma_statement)
    except sqlite3.Error:
        raise ForeignKeyEnforcementError(SanitizedDiagnostic(
            ErrorCode.FOREIGN_KEY_ENFORCEMENT_FAILED, phase, "Cannot enable foreign keys."
        )) from None
    verify_foreign_keys_enabled(conn, phase=phase)


def verify_foreign_keys_enabled(conn, *, phase=RehearsalPhase.BASELINE_VALIDATION):
    try:
        rows = conn.execute("PRAGMA foreign_keys").fetchmany(2)
    except sqlite3.Error:
        rows = []
    if rows != [(1,)]:
        raise ForeignKeyEnforcementError(SanitizedDiagnostic(
            ErrorCode.FOREIGN_KEY_ENFORCEMENT_FAILED, phase,
            "PRAGMA foreign_keys did not report exactly one enabled state."
        )) from None
