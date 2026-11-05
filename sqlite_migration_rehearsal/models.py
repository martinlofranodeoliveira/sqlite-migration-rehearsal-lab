"""Public value objects and hard resource ceilings for development 0.1.0."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlite_migration_rehearsal.errors import (
    BaselineValidationError, ErrorCode, RehearsalPhase, ResourceLimitError,
    SanitizedDiagnostic,
)

MAX_MANIFEST_BYTES = 1_048_576
MAX_BASELINE_BYTES = 16_777_216
MAX_TABLES = 32
MAX_ROWS = 10_000
MAX_STATEMENTS = 64
MAX_STATEMENT_BYTES = 16_384
MAX_INVARIANTS = 32
MAX_CELL_BYTES = 65_536
DEFAULT_MAX_VM_STEPS = 50_000
MAX_ALLOWED_VM_STEPS = 500_000
SUPPORTED_SCHEMA_VERSION = "0.1.0"
MAX_SCHEMA_OBJECTS = 256
MAX_COLUMNS = 64
MAX_INDEXES = 128
MAX_IDENTIFIER_BYTES = 128
MAX_CATALOG_BYTES = 1_048_576
MAX_STATE_BYTES = 16_777_216
MAX_REPORT_BYTES = 4_194_304
_STORAGE_TYPE_ORDER = {"null": 0, "integer": 1, "text": 2, "blob": 3}


@dataclass(frozen=True)
class RehearsalLimits:
    max_manifest_bytes: int = MAX_MANIFEST_BYTES
    max_baseline_bytes: int = MAX_BASELINE_BYTES
    max_tables: int = MAX_TABLES
    max_rows: int = MAX_ROWS
    max_statements: int = MAX_STATEMENTS
    max_statement_bytes: int = MAX_STATEMENT_BYTES
    max_invariants: int = MAX_INVARIANTS
    max_cell_bytes: int = MAX_CELL_BYTES
    max_vm_steps: int = DEFAULT_MAX_VM_STEPS

    def __post_init__(self) -> None:
        bounds = {
            "max_manifest_bytes": (1, MAX_MANIFEST_BYTES),
            "max_baseline_bytes": (4096, MAX_BASELINE_BYTES),
            "max_tables": (1, MAX_TABLES), "max_rows": (1, MAX_ROWS),
            "max_statements": (1, MAX_STATEMENTS),
            "max_statement_bytes": (64, MAX_STATEMENT_BYTES),
            "max_invariants": (0, MAX_INVARIANTS),
            "max_cell_bytes": (64, MAX_CELL_BYTES),
            "max_vm_steps": (1, MAX_ALLOWED_VM_STEPS),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer within {low}..{high}")


@dataclass(frozen=True)
class TypedValue:
    storage_type: str
    value: int | str | None

    def __post_init__(self) -> None:
        if not isinstance(self.storage_type, str) or self.storage_type not in _STORAGE_TYPE_ORDER:
            raise ValueError("Unsupported storage type")
        if self.storage_type == "null" and self.value is not None:
            raise ValueError("NULL requires None")
        if self.storage_type == "integer" and (
            type(self.value) is not int or not -(2**63) <= self.value < 2**63
        ):
            raise ValueError("INTEGER requires a signed 64-bit integer")
        if self.storage_type in ("text", "blob"):
            if not isinstance(self.value, str):
                raise ValueError("TEXT and BLOB require strings")
            try:
                self.value.encode("utf-8")
                if self.storage_type == "blob":
                    object.__setattr__(self, "value", bytes.fromhex(self.value).hex())
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Invalid text or hexadecimal value") from None

    @classmethod
    def null(cls) -> TypedValue:
        return cls("null", None)

    @classmethod
    def integer(cls, val: int) -> TypedValue:
        return cls("integer", val)

    @classmethod
    def text(cls, val: str) -> TypedValue:
        return cls("text", val)

    @classmethod
    def blob(cls, val: bytes) -> TypedValue:
        if not isinstance(val, (bytes, bytearray, memoryview)):
            raise TypeError("blob requires bytes-like input")
        return cls("blob", bytes(val).hex())

    @classmethod
    def from_sql_value(cls, raw: Any, *, max_cell_bytes: int = MAX_CELL_BYTES,
                       phase: RehearsalPhase = RehearsalPhase.BASELINE_VALIDATION,
                       table_name: str | None = None,
                       column_name: str | None = None) -> TypedValue:
        if raw is None:
            return cls.null()
        if type(raw) is int:
            return cls.integer(raw)
        if isinstance(raw, (str, bytes, bytearray, memoryview)):
            try:
                size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
            except UnicodeError:
                raise BaselineValidationError(SanitizedDiagnostic(
                    ErrorCode.UNSUPPORTED_VALUE_TYPE, phase, "Invalid UTF-8 TEXT value."
                )) from None
            if size > max_cell_bytes:
                raise ResourceLimitError(SanitizedDiagnostic(
                    ErrorCode.RESOURCE_LIMIT_EXCEEDED, phase, "Cell byte limit exceeded.",
                    object_name=table_name, details={"column": column_name}
                ))
            return cls.text(raw) if isinstance(raw, str) else cls.blob(raw)
        raise BaselineValidationError(SanitizedDiagnostic(
            ErrorCode.UNSUPPORTED_VALUE_TYPE, phase,
            "Only INTEGER, UTF-8 TEXT, NULL and BLOB values are supported.",
            object_name=table_name, details={"column": column_name}
        ))

    def sort_key(self) -> tuple[int, Any]:
        value = bytes.fromhex(str(self.value)) if self.storage_type == "blob" else self.value
        return (_STORAGE_TYPE_ORDER[self.storage_type], 0 if value is None else value)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.storage_type, "value": self.value}


@dataclass(frozen=True)
class ColumnSpec:
    cid: int
    name: str
    declared_type: str
    notnull: bool
    default_sql: str | None
    pk_index: int

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class ForeignKeySpec:
    fk_id: int
    seq: int
    target_table: str
    from_column: str
    to_column: str
    on_update: str
    on_delete: str
    match: str

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class IndexSpec:
    name: str
    unique: bool
    origin: str
    partial: bool
    columns: tuple[str, ...]
    create_sql: str | None

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "columns": list(self.columns)}


@dataclass(frozen=True)
class TableCatalog:
    name: str
    create_sql: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeySpec, ...]
    indexes: tuple[IndexSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "create_sql": self.create_sql,
                "columns": [c.to_dict() for c in self.columns],
                "primary_key": list(self.primary_key),
                "foreign_keys": [f.to_dict() for f in self.foreign_keys],
                "indexes": [i.to_dict() for i in self.indexes]}


@dataclass(frozen=True)
class CatalogState:
    tables: dict[str, TableCatalog]

    def to_dict(self) -> dict[str, Any]:
        return {n: self.tables[n].to_dict() for n in sorted(self.tables)}


@dataclass(frozen=True)
class TableState:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    rows: tuple[tuple[TypedValue, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "columns": list(self.columns),
                "primary_key": list(self.primary_key), "row_count": len(self.rows),
                "rows": [[v.to_dict() for v in row] for row in self.rows]}


@dataclass(frozen=True)
class SnapshotState:
    catalog: CatalogState
    tables: dict[str, TableState]
    total_rows: int
    state_sha256: str

    def to_summary_dict(self) -> dict[str, Any]:
        return {"table_count": len(self.catalog.tables), "total_rows": self.total_rows,
                "state_sha256": self.state_sha256, "tables": {
                    n: {"columns": list(self.tables[n].columns),
                        "primary_key": list(self.tables[n].primary_key),
                        "row_count": len(self.tables[n].rows),
                        "foreign_key_count": len(self.catalog.tables[n].foreign_keys),
                        "index_count": len(self.catalog.tables[n].indexes)}
                    for n in sorted(self.tables)}}


@dataclass(frozen=True)
class TableDiff:
    table_name: str
    matched: bool
    create_sql_matched: bool
    columns_matched: bool
    primary_key_matched: bool
    foreign_keys_matched: bool
    indexes_matched: bool
    rows_matched: bool
    expected_row_count: int
    actual_row_count: int
    missing_row_count: int
    extra_row_count: int
    modified_row_count: int

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class StateDiff:
    matched: bool
    catalog_matched: bool
    data_matched: bool
    missing_tables: tuple[str, ...]
    extra_tables: tuple[str, ...]
    table_diffs: dict[str, TableDiff]

    def to_dict(self) -> dict[str, Any]:
        return {"matched": self.matched, "catalog_matched": self.catalog_matched,
                "data_matched": self.data_matched, "missing_tables": list(self.missing_tables),
                "extra_tables": list(self.extra_tables),
                "table_diffs": {n: self.table_diffs[n].to_dict() for n in sorted(self.table_diffs)}}


@dataclass(frozen=True)
class InvariantSpec:
    invariant_id: str
    query: str
    expected: TypedValue
    description: str = ""


@dataclass(frozen=True)
class InvariantOutcome:
    invariant_id: str
    matched: bool
    expected_type: str
    actual_type: str
    vm_steps: int

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class MigrationManifest:
    schema_version: str
    migration_id: str
    description: str
    up_statements: tuple[str, ...]
    down_statements: tuple[str, ...]
    post_up_invariants: tuple[InvariantSpec, ...] = ()
    baseline_invariants: tuple[InvariantSpec, ...] = ()
    post_down_invariants: tuple[InvariantSpec, ...] = ()


@dataclass(frozen=True)
class PhaseOutcome:
    phase: RehearsalPhase
    status: str
    statements_executed: int = 0
    invariants_checked: int = 0
    vm_steps: int = 0
    transaction_committed: bool = False
    transaction_rolled_back: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**vars(self), "phase": self.phase.value}


@dataclass(frozen=True)
class RehearsalResult:
    migration_id: str
    success: bool
    status: str
    source_sha256_before: str
    source_sha256_after: str
    source_bytes: int
    source_preserved: bool
    workspace_cleaned: bool
    phases: tuple[PhaseOutcome, ...]
    invariant_outcomes: tuple[InvariantOutcome, ...] = ()
    baseline_snapshot: SnapshotState | None = None
    first_up_snapshot: SnapshotState | None = None
    down_snapshot: SnapshotState | None = None
    second_up_snapshot: SnapshotState | None = None
    rolled_back_snapshot: SnapshotState | None = None
    transaction_rollback_verified: bool | None = None
    restoration_diff: StateDiff | None = None
    reapply_diff: StateDiff | None = None
    failure: SanitizedDiagnostic | None = None

    def to_report_dict(self) -> dict[str, Any]:
        """Omit SQL and cells; identifiers and fingerprints are not anonymized."""
        snapshots = {"baseline": self.baseline_snapshot, "first_up": self.first_up_snapshot,
                     "down": self.down_snapshot, "second_up": self.second_up_snapshot,
                     "rolled_back": self.rolled_back_snapshot}
        return {"migration_id": self.migration_id, "success": self.success,
                "status": self.status, "source_preservation": {
                    "source_bytes": self.source_bytes, "sha256_before": self.source_sha256_before,
                    "sha256_after": self.source_sha256_after, "preserved": self.source_preserved},
                "workspace_cleaned": self.workspace_cleaned,
                "phases": [p.to_dict() for p in self.phases],
                "invariant_outcomes": [i.to_dict() for i in self.invariant_outcomes],
                "snapshots": {n: s.to_summary_dict() if s is not None else None
                              for n, s in snapshots.items()},
                "transaction_rollback_verified": self.transaction_rollback_verified,
                "restoration_diff": self.restoration_diff.to_dict() if self.restoration_diff else None,
                "reapply_diff": self.reapply_diff.to_dict() if self.reapply_diff else None,
                "failure": self.failure.to_dict() if self.failure else None}
