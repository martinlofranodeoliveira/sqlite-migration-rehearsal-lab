"""SQLite Migration Rehearsal Lab.

Disposable SQLite migration rehearsals using consistent read-only backups,
explicit scalar invariants, and up/down/up logical state comparisons.
"""

from sqlite_migration_rehearsal.catalog import compare_snapshots, inspect_snapshot
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.errors import (
    BaselineValidationError,
    ErrorCode,
    ForeignKeyEnforcementError,
    ForeignKeyViolationError,
    InvariantViolationError,
    ManifestValidationError,
    ReapplyMismatchError,
    RehearsalError,
    RehearsalPhase,
    ResourceLimitError,
    RestorationMismatchError,
    SanitizedDiagnostic,
    SqlExecutionError,
    StatementGuardError,
    StepBudgetExceededError,
)
from sqlite_migration_rehearsal.manifest import (
    load_manifest_file,
    parse_manifest_bytes,
    parse_manifest_dict,
)
from sqlite_migration_rehearsal.models import (
    CatalogState,
    ColumnSpec,
    ForeignKeySpec,
    IndexSpec,
    InvariantOutcome,
    InvariantSpec,
    MigrationManifest,
    PhaseOutcome,
    RehearsalLimits,
    RehearsalResult,
    SnapshotState,
    StateDiff,
    TableCatalog,
    TableDiff,
    TableState,
    TypedValue,
)
from sqlite_migration_rehearsal.workspace import (
    compute_file_sha256,
    enable_and_verify_foreign_keys,
    verify_foreign_keys_enabled,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BaselineValidationError",
    "CatalogState",
    "ColumnSpec",
    "ErrorCode",
    "ForeignKeyEnforcementError",
    "ForeignKeySpec",
    "ForeignKeyViolationError",
    "IndexSpec",
    "InvariantOutcome",
    "InvariantSpec",
    "InvariantViolationError",
    "ManifestValidationError",
    "MigrationManifest",
    "PhaseOutcome",
    "ReapplyMismatchError",
    "RehearsalError",
    "RehearsalLimits",
    "RehearsalPhase",
    "RehearsalResult",
    "ResourceLimitError",
    "RestorationMismatchError",
    "SanitizedDiagnostic",
    "SnapshotState",
    "SqlExecutionError",
    "StateDiff",
    "StatementGuardError",
    "StepBudgetExceededError",
    "TableCatalog",
    "TableDiff",
    "TableState",
    "TypedValue",
    "compare_snapshots",
    "compute_file_sha256",
    "enable_and_verify_foreign_keys",
    "inspect_snapshot",
    "load_manifest_file",
    "parse_manifest_bytes",
    "parse_manifest_dict",
    "rehearse_migration",
    "verify_foreign_keys_enabled",
]
