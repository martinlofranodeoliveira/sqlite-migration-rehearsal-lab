"""Bounded CLI output without raw SQLite exceptions, SQL, cell values or paths."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlite_migration_rehearsal.demo import create_users_basic_baseline, format_demo_human, run_demo
from sqlite_migration_rehearsal.engine import rehearse_migration
from sqlite_migration_rehearsal.errors import RehearsalError
from sqlite_migration_rehearsal.manifest import load_manifest_file
from sqlite_migration_rehearsal.models import MAX_REPORT_BYTES, RehearsalLimits


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid command arguments; use --help.\n")


def build_parser():
    parser = SafeParser(prog="sqlite_migration_rehearsal", description="Disposable trusted SQLite migration rehearsal.")
    subs = parser.add_subparsers(dest="command", required=True)
    demo = subs.add_parser("demo", help="Run synthetic offline scenarios.")
    demo.add_argument("--json", action="store_true")
    run = subs.add_parser("run", help="Rehearse an explicit baseline and manifest.")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--baseline")
    source.add_argument("--seed", choices=["users_basic"])
    run.add_argument("--manifest", required=True)
    run.add_argument("--max-vm-steps", type=int, default=50000)
    run.add_argument("--json", action="store_true")
    validate = subs.add_parser("validate-manifest", help="Validate syntax and policy without running SQL.")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--json", action="store_true")
    return parser


def _emit(payload):
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    if len(text.encode("ascii")) > MAX_REPORT_BYTES:
        sys.stdout.write('{"success":false,"error":"REPORT_LIMIT_EXCEEDED"}\n')
        return False
    sys.stdout.write(text + "\n")
    return True


def format_result_human(result):
    lines = [f"Migration: {result.migration_id}", f"Status: {result.status}",
             f"Source bytes preserved: {result.source_preserved}",
             f"Owned workspace removed: {result.workspace_cleaned}"]
    if result.failure:
        lines.append(f"Failure: {result.failure.code.value} at {result.failure.phase.value}")
    return "\n".join(lines)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            summary = run_demo()
            if args.json:
                if not _emit(summary):
                    return 1
            else:
                sys.stdout.write(format_demo_human(summary) + "\n")
            return 0 if summary["all_expectations_met"] else 1
        limits = RehearsalLimits(max_vm_steps=getattr(args, "max_vm_steps", 50000))
        manifest = load_manifest_file(args.manifest, limits=limits)
        if args.command == "validate-manifest":
            if args.json:
                _emit({"valid": True, "migration_id": manifest.migration_id,
                       "up_statement_count": len(manifest.up_statements),
                       "down_statement_count": len(manifest.down_statements),
                       "post_up_invariant_count": len(manifest.post_up_invariants)})
            else:
                sys.stdout.write("Manifest valid: " + manifest.migration_id + "\n")
            return 0
        if args.seed:
            with tempfile.TemporaryDirectory(prefix="sqlite_rehearsal_seed_") as directory:
                baseline = create_users_basic_baseline(Path(directory) / "seed.sqlite3")
                result = rehearse_migration(baseline, manifest, limits=limits, workspace_parent_dir=directory)
        else:
            result = rehearse_migration(args.baseline, manifest, limits=limits)
        if args.json:
            if not _emit(result.to_report_dict()):
                return 1
        else:
            sys.stdout.write(format_result_human(result) + "\n")
        return 0 if result.success else 1
    except RehearsalError as exc:
        if args.json:
            _emit({"success": False, "failure": exc.diagnostic.to_dict()})
        else:
            sys.stderr.write("Input error: " + exc.diagnostic.code.value + "\n")
        return 2
    except (OSError, sqlite3.Error, ValueError, TypeError):
        if args.json:
            _emit({"success": False, "error": "INPUT_OR_RESOURCE_ERROR"})
        else:
            sys.stderr.write("Cannot access or configure rehearsal resources.\n")
        return 2
