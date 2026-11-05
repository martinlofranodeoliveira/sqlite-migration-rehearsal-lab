"""Synthetic, offline demonstrations. Only owned temporary directories are removed."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.manifest import parse_manifest_dict
from sqlite_migration_rehearsal.models import RehearsalLimits
from sqlite_migration_rehearsal.workspace import compute_file_sha256


def create_users_basic_baseline(db_path):
    """Exclusively create a synthetic seed; never overwrite an existing caller file."""
    target = Path(db_path)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    conn = sqlite3.connect(str(target))
    try:
        with conn:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
            conn.executemany("INSERT INTO users (id,name) VALUES (?,?)", [(1, "Ada"), (2, "Lin")])
    finally:
        conn.close()
    return target


def _payload(lossy=False):
    return {
        "schema_version": "0.1.0",
        "migration_id": "2026_02_users_add_status_lossy_down" if lossy else "2026_01_users_add_status_reversible",
        "up_statements": ["ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"],
        "post_up_invariants": [{"id": "counts_2_active", "query": "SELECT COUNT(*) FROM users WHERE status = 'active'",
                                "expected": {"type": "integer", "value": 2}}],
        "down_statements": [
            "CREATE TABLE users_down_staging (id INTEGER PRIMARY KEY, name TEXT)",
            "INSERT INTO users_down_staging (id, name) SELECT id, name FROM users" + (" WHERE id = 1" if lossy else ""),
            "DROP TABLE users",
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
            "INSERT INTO users (id, name) SELECT id, name FROM users_down_staging",
            "DROP TABLE users_down_staging",
        ],
    }


def build_reversible_users_status_manifest():
    return parse_manifest_dict(_payload())


def build_lossy_down_users_manifest():
    return parse_manifest_dict(_payload(True))


def build_failing_up_after_update_manifest():
    payload = _payload()
    payload["migration_id"] = "2026_03_users_failing_up_after_update"
    payload["up_statements"] = ["UPDATE users SET name = 'Ada Updated' WHERE id = 1",
                                "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"]
    return parse_manifest_dict(payload)


def build_sql_failure_after_update_manifest():
    payload = _payload()
    payload["migration_id"] = "2026_04_sql_failure_after_update"
    payload["up_statements"] = ["UPDATE users SET name = 'Ada Updated' WHERE id = 1",
                                "INSERT INTO missing_table VALUES (1)"]
    return parse_manifest_dict(payload)


def run_demo(*, limits=None):
    active = limits or RehearsalLimits()
    reports = []
    with tempfile.TemporaryDirectory(prefix="sqlite_rehearsal_demo_") as directory:
        root = Path(directory)
        baseline = create_users_basic_baseline(root / "users.sqlite3")
        original = baseline.read_bytes()
        before = compute_file_sha256(baseline)
        scenarios = [
            ("reversible_up_down_up", build_reversible_users_status_manifest(), None),
            ("lossy_down_drops_row", build_lossy_down_users_manifest(), "RESTORATION_MISMATCH"),
            ("failing_up_after_update_rolls_back", build_failing_up_after_update_manifest(), "INVARIANT_MISMATCH"),
            ("sql_failure_after_update_rolls_back", build_sql_failure_after_update_manifest(), "SQL_EXECUTION_ERROR"),
        ]
        for name, manifest, expected_code in scenarios:
            result = rehearse_migration(baseline, manifest, limits=active, workspace_parent_dir=root)
            matched = result.success if expected_code is None else (
                result.failure is not None and result.failure.code.value == expected_code
            )
            if expected_code in ("INVARIANT_MISMATCH", "SQL_EXECUTION_ERROR"):
                matched = matched and result.transaction_rollback_verified is True
            reports.append({"scenario": name, "expected_outcome": expected_code or "passed",
                            "actual_outcome": result.status, "matched_expectation": bool(matched),
                            "report": result.to_report_dict()})
        after = compute_file_sha256(baseline)
        preserved = baseline.read_bytes() == original and before == after
        size = len(original)
    cleaned = not root.exists()
    return {"demo": "sqlite-migration-rehearsal-lab", "version": "0.1.0",
            "all_expectations_met": preserved and cleaned and all(r["matched_expectation"] for r in reports),
            "source_baseline": {"bytes": size, "sha256_before": before, "sha256_after": after, "preserved": preserved},
            "demo_workspace_cleaned": cleaned, "scenarios": reports}


def format_demo_human(summary):
    lines = ["SQLite Migration Rehearsal Lab 0.1.0 (development)",
             "Expectations: " + ("PASS" if summary["all_expectations_met"] else "FAIL"),
             "Source bytes preserved: " + str(summary["source_baseline"]["preserved"]),
             "Owned workspace removed: " + str(summary["demo_workspace_cleaned"])]
    for scenario in summary["scenarios"]:
        failure = scenario["report"]["failure"]
        outcome = failure["code"] if failure else "passed"
        lines.append(scenario["scenario"] + ": " + outcome)
    return "\n".join(lines)


def format_demo_json(summary):
    return json.dumps(summary, sort_keys=True, indent=2)
