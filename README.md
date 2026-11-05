# SQLite Migration Rehearsal Lab

**Development version 0.1.0 — unreleased.** Original MIT software by [Lofrano Tecnologia](https://lofranotecnologia.com.br).

Rehearse trusted SQLite migration statements on a disposable consistent backup: baseline checks → up → explicit scalar invariants → commit → down → restoration comparison → reapply up → repeatability comparison. The source connection is read-only. This is a local integration utility, not a deployment tool or a SQL-equivalence prover.

[Português](README.pt-BR.md) · [Contract](docs/contract.md) · [Design](docs/design.md)

## Run without installation

Requires Python 3.13+ with SQLite 3.35+; the reference environment is `python:3.13-slim-bookworm`. Runtime, tests and demo use only the standard library and require no network, model, API, credentials or home-directory configuration. From the project directory:

```sh
python3 -m unittest discover -s tests -v
python3 -m sqlite_migration_rehearsal demo
python3 -m sqlite_migration_rehearsal demo --json
python3 -m sqlite_migration_rehearsal validate-manifest --manifest fixtures/manifests/users_add_status_reversible.json
python3 -m sqlite_migration_rehearsal run --seed users_basic --manifest fixtures/manifests/users_add_status_reversible.json --json
```

Human demo output:

```text
SQLite Migration Rehearsal Lab 0.1.0 (development)
Expectations: PASS
Source bytes preserved: True
Owned workspace removed: True
reversible_up_down_up: passed
lossy_down_drops_row: RESTORATION_MISMATCH
failing_up_after_update_rolls_back: INVARIANT_MISMATCH
sql_failure_after_update_rolls_back: SQL_EXECUTION_ERROR
```

JSON additionally includes phase outcomes, logical fingerprints, sanitized snapshot summaries, comparison counts and rollback verification. VM counts and SQLite file fingerprints may differ across SQLite versions. No temporary path or timestamp appears in the demo report.

## Optional installation

Build dependency preparation is separate from offline checks. This command uses real pinned build tooling in an isolated environment and may access the package index:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m sqlite_migration_rehearsal demo
```

Setuptools 80.9.0 is a build dependency only. There are no runtime dependencies. An installed console entry point is `sqlite-migration-rehearsal`.

## Explicit baseline

Choose a trusted, finite local SQLite file in trusted parent directories:

```sh
python3 -m sqlite_migration_rehearsal run --baseline ./synthetic.sqlite3 --manifest fixtures/manifests/users_add_status_reversible.json --max-vm-steps 50000
```

The bundled seed contains only synthetic `(1, 'Ada')` and `(2, 'Lin')`. No real personal data is included. An existing caller file is never overwritten by the seed helper.

### Library example

```python
from sqlite_migration_rehearsal import load_manifest_file, rehearse_migration

manifest = load_manifest_file("fixtures/manifests/users_add_status_reversible.json")
result = rehearse_migration("synthetic.sqlite3", manifest)
print(result.status)
print(result.to_report_dict()["restoration_diff"])
```

File/manifest parsing raises sanitized `RehearsalError` on invalid input. `rehearse_migration` returns a failed `RehearsalResult` for expected input, SQLite and filesystem failures. Resource-configuration mistakes raise `ValueError` when constructing `RehearsalLimits`.

## Why these comparisons?

A down migration can preserve every object name and still lose a row. The demo deliberately omits Lin and reports restoration failure. Tests also cover a parent-table rebuild that cascades away a child row. The valid FK example instead drops the added column without deleting its parent table. Failed SQL after an earlier UPDATE and failed scalar invariants both explicitly roll back the disposable transaction; neither is a developer-written down migration.

Comparison includes exact stored SQL, structured columns, PK/FK/index metadata and primary-key-sorted typed rows. No whitespace rewriting is used. Semantically equivalent schemas may compare unequal, intentionally.

## Limits and safety

Defaults: manifest 1 MiB, source plus sidecars 16 MiB, 32 tables, 10,000 total rows, 64 statements **per direction**, 16,384 UTF-8 bytes per statement, 32 invariants per list and 50,000 VM steps per statement batch, invariant or inspection. Additional schema, row, value, serialization and backup budgets are specified in the [contract](docs/contract.md).

Only ordinary rowid tables with explicit, actually nonnull and unique supported primary keys are supported. INTEGER/TEXT/NULL/BLOB cells are supported; REAL, views, triggers, virtual/generated objects, WITHOUT ROWID, expression indexes, custom collations and SQLite internal state tables are rejected. SQL functions use a small allowlist.

Trusted SQL is **not a security sandbox**. A VM step is not a memory or wall-clock limit; SQLite functions, preparation, locking and filesystem operations can be expensive. Use external process memory/CPU/time limits for costly inputs. Backup has independent page and retry/callback budgets. Parent directories must be trusted, and callers must not race file replacement. External writers are not stopped; main-file SHA-256 is evidence only for quiescent fixtures, not a complete WAL-state identity.

CLI/report summaries omit raw SQL and cell values but retain identifiers and fingerprints; escaping is not anonymization. Library snapshots intentionally contain all supplied supported rows and catalog SQL. Handle them as caller data. Cleanup failure is reported, and an owned temporary workspace may remain for host-level cleanup; no arbitrary caller directory is removed.

CLI exit codes: 0 expected success (including expected demo failures), 1 failed rehearsal/demo/output budget, 2 invalid CLI/manifest/configuration or resource setup outside a rehearsal. No telemetry or remote deployment exists.

Security reports: [GitHub private vulnerability reporting](https://github.com/martinlofranodeoliveira/sqlite-migration-rehearsal-lab/security/advisories/new) when available. If unavailable, request a private route through the [official homepage](https://lofranotecnologia.com.br).
