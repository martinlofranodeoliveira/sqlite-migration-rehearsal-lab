"""Error taxonomy and sanitized diagnostics for SQLite migration rehearsals.

Diagnostics intentionally omit raw SQL statements and database cell values.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Stable machine-readable failure codes for rehearsal operations."""

    INVALID_BASELINE = "INVALID_BASELINE"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    MANIFEST_VALIDATION_ERROR = "MANIFEST_VALIDATION_ERROR"
    STATEMENT_REJECTED = "STATEMENT_REJECTED"
    FOREIGN_KEY_ENFORCEMENT_FAILED = "FOREIGN_KEY_ENFORCEMENT_FAILED"
    FOREIGN_KEY_VIOLATION = "FOREIGN_KEY_VIOLATION"
    INTEGRITY_CHECK_FAILED = "INTEGRITY_CHECK_FAILED"
    UNSUPPORTED_SCHEMA_OBJECT = "UNSUPPORTED_SCHEMA_OBJECT"
    UNSUPPORTED_TABLE_KEY = "UNSUPPORTED_TABLE_KEY"
    INVALID_PRIMARY_KEY_DATA = "INVALID_PRIMARY_KEY_DATA"
    UNSUPPORTED_VALUE_TYPE = "UNSUPPORTED_VALUE_TYPE"
    STEP_BUDGET_EXCEEDED = "STEP_BUDGET_EXCEEDED"
    SQL_EXECUTION_ERROR = "SQL_EXECUTION_ERROR"
    INVARIANT_INVALID_QUERY = "INVARIANT_INVALID_QUERY"
    INVARIANT_SHAPE_ERROR = "INVARIANT_SHAPE_ERROR"
    INVARIANT_MISMATCH = "INVARIANT_MISMATCH"
    RESTORATION_MISMATCH = "RESTORATION_MISMATCH"
    REAPPLY_MISMATCH = "REAPPLY_MISMATCH"
    SOURCE_MUTATION_DETECTED = "SOURCE_MUTATION_DETECTED"


class RehearsalPhase(str, Enum):
    """Ordered execution phases of a migration rehearsal."""

    SOURCE_PREPARATION = "source_preparation"
    MANIFEST_VALIDATION = "manifest_validation"
    BASELINE_VALIDATION = "baseline_validation"
    UP_MIGRATION = "up_migration"
    POST_UP_INVARIANTS = "post_up_invariants"
    DOWN_MIGRATION = "down_migration"
    RESTORATION_COMPARISON = "restoration_comparison"
    REAPPLY_UP_MIGRATION = "reapply_up_migration"
    REAPPLY_COMPARISON = "reapply_comparison"
    SOURCE_VERIFICATION = "source_verification"


@dataclass(frozen=True)
class SanitizedDiagnostic:
    """Structured diagnostic guaranteed not to contain raw SQL or cell values."""

    code: ErrorCode
    phase: RehearsalPhase
    message: str
    statement_index: int | None = None
    invariant_id: str | None = None
    object_name: str | None = None
    transaction_rolled_back: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "phase": self.phase.value,
            "message": self.message,
            "statement_index": self.statement_index,
            "invariant_id": self.invariant_id,
            "object_name": self.object_name,
            "transaction_rolled_back": self.transaction_rolled_back,
            "details": dict(self.details),
        }


class RehearsalError(Exception):
    """Base exception carrying a sanitized diagnostic."""

    def __init__(self, diagnostic: SanitizedDiagnostic) -> None:
        super().__init__(f"[{diagnostic.code.value}] ({diagnostic.phase.value}) {diagnostic.message}")
        self.diagnostic = diagnostic

    def with_rollback_status(self, rolled_back: bool) -> RehearsalError:
        updated = SanitizedDiagnostic(
            code=self.diagnostic.code,
            phase=self.diagnostic.phase,
            message=self.diagnostic.message,
            statement_index=self.diagnostic.statement_index,
            invariant_id=self.diagnostic.invariant_id,
            object_name=self.diagnostic.object_name,
            transaction_rolled_back=rolled_back,
            details=dict(self.diagnostic.details),
        )
        return self.__class__(updated)


class BaselineValidationError(RehearsalError):
    """Raised when a baseline or snapshot violates schema or data constraints."""


class ResourceLimitError(RehearsalError):
    """Raised when a file, manifest, table, row, or byte limit is exceeded."""


class ManifestValidationError(RehearsalError):
    """Raised when a migration manifest is malformed or violates bounds."""


class StatementGuardError(RehearsalError):
    """Raised when a SQL statement attempts a prohibited operation."""


class ForeignKeyEnforcementError(RehearsalError):
    """Raised when PRAGMA foreign_keys enforcement cannot be enabled or verified."""


class ForeignKeyViolationError(RehearsalError):
    """Raised when immediate or deferred foreign-key constraints are violated."""


class InvariantViolationError(RehearsalError):
    """Raised when a declared scalar invariant query has bad shape or mismatches expected value."""


class StepBudgetExceededError(RehearsalError):
    """Raised when SQLite virtual machine execution exceeds the configured step budget."""


class RestorationMismatchError(RehearsalError):
    """Raised when post-down state does not match the initial baseline state."""


class ReapplyMismatchError(RehearsalError):
    """Raised when the second up state does not match the first up state."""


class SqlExecutionError(RehearsalError):
    """Raised when SQLite rejects a statement during execution."""


def classify_sqlite_error(
    exc: sqlite3.Error,
    *,
    phase: RehearsalPhase,
    statement_index: int | None = None,
    invariant_id: str | None = None,
) -> RehearsalError:
    """Convert a raw sqlite3.Error into a sanitized RehearsalError without leaking SQL or values."""

    raw_lower = str(exc).lower()
    if isinstance(exc, sqlite3.IntegrityError) and "foreign key constraint failed" in raw_lower:
        return ForeignKeyViolationError(
            SanitizedDiagnostic(
                code=ErrorCode.FOREIGN_KEY_VIOLATION,
                phase=phase,
                message="Foreign-key constraint failed during statement execution.",
                statement_index=statement_index,
                invariant_id=invariant_id,
                details={"sqlite_error_class": type(exc).__name__},
            )
        )

    if isinstance(exc, sqlite3.IntegrityError):
        category = "integrity_constraint"
        if "not null constraint failed" in raw_lower:
            category = "not_null_constraint"
        elif "unique constraint failed" in raw_lower or "primary key" in raw_lower:
            category = "unique_or_pk_constraint"
        elif "check constraint failed" in raw_lower:
            category = "check_constraint"
        return SqlExecutionError(
            SanitizedDiagnostic(
                code=ErrorCode.SQL_EXECUTION_ERROR,
                phase=phase,
                message=f"SQLite integrity constraint failed ({category}).",
                statement_index=statement_index,
                invariant_id=invariant_id,
                details={
                    "sqlite_error_class": type(exc).__name__,
                    "category": category,
                },
            )
        )

    if "database or disk is full" in raw_lower:
        return ResourceLimitError(
            SanitizedDiagnostic(
                code=ErrorCode.RESOURCE_LIMIT_EXCEEDED,
                phase=phase,
                message="Working database exceeded maximum allowed page capacity.",
                statement_index=statement_index,
                invariant_id=invariant_id,
                details={"sqlite_error_class": type(exc).__name__},
            )
        )

    category = "execution_error"
    if "syntax error" in raw_lower or "incomplete input" in raw_lower:
        category = "syntax_error"
    elif "no such table" in raw_lower:
        category = "missing_table"
    elif "no such column" in raw_lower:
        category = "missing_column"
    elif "already exists" in raw_lower:
        category = "object_already_exists"
    elif "duplicate column" in raw_lower:
        category = "duplicate_column"

    return SqlExecutionError(
        SanitizedDiagnostic(
            code=ErrorCode.SQL_EXECUTION_ERROR,
            phase=phase,
            message=f"SQLite rejected statement execution ({category}).",
            statement_index=statement_index,
            invariant_id=invariant_id,
            details={
                "sqlite_error_class": type(exc).__name__,
                "category": category,
            },
        )
    )
