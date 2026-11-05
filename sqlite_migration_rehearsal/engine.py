"""Engine-owned transactions over a disposable consistent copy."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from sqlite_migration_rehearsal.catalog import compare_snapshots, inspect_snapshot
from sqlite_migration_rehearsal.errors import (
    ErrorCode, InvariantViolationError, RehearsalError, RehearsalPhase,
    SanitizedDiagnostic, StatementGuardError, StepBudgetExceededError, classify_sqlite_error,
)
from sqlite_migration_rehearsal.manifest import revalidate_manifest
from sqlite_migration_rehearsal.models import (
    InvariantOutcome, MigrationManifest, PhaseOutcome, RehearsalLimits, RehearsalResult, TypedValue,
)
from sqlite_migration_rehearsal.sql_guard import (
    MigrationAuthorizer, ProgressStepTracker, ReadOnlyInvariantAuthorizer,
    validate_invariant_query, validate_migration_statement,
)
from sqlite_migration_rehearsal.workspace import (
    backup_source_to_workspace, compute_file_sha256, enable_and_verify_foreign_keys,
    validate_source_database_file, verify_foreign_keys_enabled,
)


def _execution_error(exc, tracker, authorizer, phase, index=None, invariant_id=None):
    if tracker.exceeded:
        return StepBudgetExceededError(SanitizedDiagnostic(
            ErrorCode.STEP_BUDGET_EXCEEDED, phase, "SQLite VM step budget exceeded.",
            statement_index=index, invariant_id=invariant_id
        ))
    if authorizer.denied_reason:
        return StatementGuardError(SanitizedDiagnostic(
            ErrorCode.INVARIANT_INVALID_QUERY if invariant_id else ErrorCode.STATEMENT_REJECTED,
            phase, "SQLite authorizer denied a prohibited operation.",
            statement_index=index, invariant_id=invariant_id,
            details={"reason": authorizer.denied_reason}
        ))
    return classify_sqlite_error(exc, phase=phase, statement_index=index, invariant_id=invariant_id)


def _execute_migration_statements(conn, statements, *, limits, phase):
    tracker = ProgressStepTracker(limits.max_vm_steps)
    authorizer = MigrationAuthorizer()
    conn.set_authorizer(authorizer)
    conn.set_progress_handler(tracker, 1)
    try:
        for index, sql in enumerate(statements):
            validate_migration_statement(sql, max_statement_bytes=limits.max_statement_bytes,
                                         phase=phase, statement_index=index)
            authorizer.begin_statement()
            try:
                cursor = conn.execute(sql)
                try:
                    # Fully step RETURNING/CTE SELECT results without retaining or printing them.
                    returned = 0
                    while cursor.fetchone() is not None:
                        returned += 1
                        if returned > limits.max_rows:
                            raise StatementGuardError(SanitizedDiagnostic(
                                ErrorCode.RESOURCE_LIMIT_EXCEEDED, phase,
                                "Migration result row limit exceeded.", statement_index=index
                            ))
                finally:
                    cursor.close()
            except sqlite3.Error as exc:
                raise _execution_error(exc, tracker, authorizer, phase, index) from None
            if not conn.in_transaction:
                raise StatementGuardError(SanitizedDiagnostic(
                    ErrorCode.STATEMENT_REJECTED, phase, "Migration ended the engine-owned transaction.",
                    statement_index=index
                ))
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    return tracker.steps


def _evaluate_invariants(conn, invariants, *, limits, phase, outcomes_sink):
    total = 0
    for inv in invariants:
        validate_invariant_query(inv.query, max_statement_bytes=limits.max_statement_bytes,
                                 phase=phase, invariant_id=inv.invariant_id)
        tracker = ProgressStepTracker(limits.max_vm_steps)
        authorizer = ReadOnlyInvariantAuthorizer()
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(tracker, 1)
        try:
            try:
                cursor = conn.execute(inv.query)
                try:
                    rows = cursor.fetchmany(2)
                finally:
                    cursor.close()
            except sqlite3.Error as exc:
                raise _execution_error(exc, tracker, authorizer, phase,
                                       invariant_id=inv.invariant_id) from None
        finally:
            conn.set_authorizer(None)
            conn.set_progress_handler(None, 0)
        total += tracker.steps
        if len(rows) != 1 or len(rows[0]) != 1:
            raise InvariantViolationError(SanitizedDiagnostic(
                ErrorCode.INVARIANT_SHAPE_ERROR, phase, "Invariant must return exactly one row and one column.",
                invariant_id=inv.invariant_id
            ))
        value = TypedValue.from_sql_value(rows[0][0], max_cell_bytes=limits.max_cell_bytes, phase=phase)
        matched = value == inv.expected
        outcomes_sink.append(InvariantOutcome(inv.invariant_id, matched, inv.expected.storage_type,
                                              value.storage_type, tracker.steps))
        if not matched:
            raise InvariantViolationError(SanitizedDiagnostic(
                ErrorCode.INVARIANT_MISMATCH, phase, "Invariant did not match its expected typed scalar.",
                invariant_id=inv.invariant_id
            ))
    return total


def _rollback_safely(conn):
    try:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        # Some SQLite failures automatically roll back; logical verification is separate.
        return not conn.in_transaction
    except sqlite3.Error:
        return False


def rehearse_migration(baseline_path: str | os.PathLike[str], manifest: MigrationManifest, *,
                       limits: RehearsalLimits | None = None,
                       workspace_parent_dir: str | os.PathLike[str] | None = None) -> RehearsalResult:
    active = limits or RehearsalLimits()
    phases = []
    outcomes = []
    states = {"baseline": None, "first_up": None, "down": None, "second_up": None, "rolled_back": None}
    restoration = reapply = None
    rollback_verified = None
    failure = None
    source = workspace = conn = None
    before = after = ""
    source_bytes = 0
    cleaned = True
    current_phase = RehearsalPhase.MANIFEST_VALIDATION
    migration_id = "invalid_manifest"
    try:
        manifest = revalidate_manifest(manifest, limits=active)
        migration_id = manifest.migration_id
        current_phase = RehearsalPhase.SOURCE_PREPARATION
        source, source_bytes, before = validate_source_database_file(baseline_path, limits=active)
        workspace = Path(tempfile.mkdtemp(prefix="sqlite_rehearsal_", dir=workspace_parent_dir))
        cleaned = False
        conn = backup_source_to_workspace(source, workspace / "working.sqlite3", limits=active)
        phases.append(PhaseOutcome(current_phase, "passed"))
        current_phase = RehearsalPhase.BASELINE_VALIDATION
        enable_and_verify_foreign_keys(conn, phase=current_phase)
        states["baseline"] = inspect_snapshot(conn, limits=active, phase=current_phase)
        steps = _evaluate_invariants(conn, manifest.baseline_invariants, limits=active,
                                     phase=current_phase, outcomes_sink=outcomes)
        phases.append(PhaseOutcome(current_phase, "passed", invariants_checked=len(manifest.baseline_invariants), vm_steps=steps))

        def transaction(key, statements, invariants, phase, inv_phase, previous):
            nonlocal current_phase, rollback_verified
            current_phase = phase
            try:
                verify_foreign_keys_enabled(conn, phase=phase)
                conn.execute("BEGIN IMMEDIATE")
                steps = _execute_migration_statements(conn, statements, limits=active, phase=phase)
                snapshot = inspect_snapshot(conn, limits=active, phase=phase)
                current_phase = inv_phase
                inv_steps = _evaluate_invariants(conn, invariants, limits=active, phase=inv_phase, outcomes_sink=outcomes)
                current_phase = phase
                conn.execute("COMMIT")
                states[key] = snapshot
                if inv_phase != phase:
                    phases.append(PhaseOutcome(phase, "passed", len(statements), vm_steps=steps, transaction_committed=True))
                    phases.append(PhaseOutcome(inv_phase, "passed", invariants_checked=len(invariants),
                                               vm_steps=inv_steps, transaction_committed=True))
                else:
                    phases.append(PhaseOutcome(phase, "passed", len(statements), len(invariants),
                                               steps + inv_steps, transaction_committed=True))
            except (RehearsalError, sqlite3.Error) as exc:
                error = exc if isinstance(exc, RehearsalError) else classify_sqlite_error(exc, phase=current_phase)
                ended = _rollback_safely(conn)
                rollback_verified = False
                if ended:
                    try:
                        states["rolled_back"] = inspect_snapshot(conn, limits=active, phase=phase)
                        rollback_verified = compare_snapshots(states[previous], states["rolled_back"]).matched
                    except RehearsalError:
                        pass
                raise error.with_rollback_status(ended) from None

        transaction("first_up", manifest.up_statements, manifest.post_up_invariants,
                    RehearsalPhase.UP_MIGRATION, RehearsalPhase.POST_UP_INVARIANTS, "baseline")
        transaction("down", manifest.down_statements, manifest.post_down_invariants,
                    RehearsalPhase.DOWN_MIGRATION, RehearsalPhase.DOWN_MIGRATION, "first_up")
        current_phase = RehearsalPhase.RESTORATION_COMPARISON
        restoration = compare_snapshots(states["baseline"], states["down"])
        if not restoration.matched:
            raise RehearsalError(SanitizedDiagnostic(
                ErrorCode.RESTORATION_MISMATCH, current_phase,
                "Post-down catalog or typed rows differ from the baseline."
            ))
        phases.append(PhaseOutcome(current_phase, "passed"))
        transaction("second_up", manifest.up_statements, manifest.post_up_invariants,
                    RehearsalPhase.REAPPLY_UP_MIGRATION, RehearsalPhase.REAPPLY_UP_MIGRATION, "down")
        current_phase = RehearsalPhase.REAPPLY_COMPARISON
        reapply = compare_snapshots(states["first_up"], states["second_up"])
        if not reapply.matched:
            raise RehearsalError(SanitizedDiagnostic(
                ErrorCode.REAPPLY_MISMATCH, current_phase,
                "Second post-up catalog or typed rows differ from the first."
            ))
        phases.append(PhaseOutcome(current_phase, "passed"))
    except RehearsalError as exc:
        failure = exc.diagnostic
    except sqlite3.Error as exc:
        failure = classify_sqlite_error(exc, phase=current_phase).diagnostic
    except (OSError, TypeError, ValueError, UnicodeError):
        failure = SanitizedDiagnostic(ErrorCode.INVALID_BASELINE, current_phase,
                                      "Cannot access the requested rehearsal resources.")
    finally:
        if conn is not None:
            try:
                _rollback_safely(conn)
                conn.close()
            except sqlite3.Error:
                if failure is None:
                    failure = SanitizedDiagnostic(ErrorCode.SQL_EXECUTION_ERROR, current_phase,
                                                  "Cannot close the disposable database.")
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
                cleaned = True
            except OSError:
                cleaned = False
                if failure is None:
                    failure = SanitizedDiagnostic(ErrorCode.SQL_EXECUTION_ERROR, current_phase,
                                                  "Cannot remove the owned temporary workspace.")
        if source is not None:
            try:
                after = compute_file_sha256(source, max_bytes=active.max_baseline_bytes)
            except RehearsalError:
                if failure is None:
                    failure = SanitizedDiagnostic(ErrorCode.SOURCE_MUTATION_DETECTED,
                                                  RehearsalPhase.SOURCE_VERIFICATION,
                                                  "Cannot verify the source fingerprint.")
    preserved = bool(before and after and before == after)
    if source is not None and not preserved and failure is None:
        failure = SanitizedDiagnostic(ErrorCode.SOURCE_MUTATION_DETECTED,
                                      RehearsalPhase.SOURCE_VERIFICATION,
                                      "Source fingerprint changed during rehearsal.")
    if failure:
        phases.append(PhaseOutcome(failure.phase, "failed", transaction_rolled_back=failure.transaction_rolled_back))
    success = failure is None and preserved and cleaned
    return RehearsalResult(
        migration_id, success, "passed" if success else "failed", before, after, source_bytes,
        preserved, cleaned, tuple(phases), tuple(outcomes), states["baseline"], states["first_up"],
        states["down"], states["second_up"], states["rolled_back"], rollback_verified,
        restoration, reapply, failure
    )
