# Development 0.1.0 contract

This version is unreleased. Supported behavior is the subset below, not general SQLite compatibility or SQL equivalence.

## Inputs and API

`load_manifest_file(path, limits=...)`, `parse_manifest_bytes(bytes, limits=...)` and `parse_manifest_dict(dict, limits=...)` return `MigrationManifest` or raise a sanitized `RehearsalError`. `rehearse_migration(baseline_path, manifest, limits=..., workspace_parent_dir=...)` revalidates the manifest, then returns `RehearsalResult` for expected rehearsal failures. Dataclasses are value objects, not proof that an input was validated. `RehearsalLimits` accepts strict integers in its documented ranges, not floats or booleans.

The baseline must be an explicit trusted regular SQLite file, or the CLI may materialize the synthetic `users_basic` seed. Final-component symlinks, directories and special files are refused. Parent directories must be trusted. File replacement races and hostile/infinite filesystems are outside this contract. Source is opened with a percent-encoded file URI and `mode=ro`; migration SQL is never executed on that connection. Backup uses `Connection.backup`, not a byte copy. Only a newly exclusively created destination within an owned temporary workspace is used.

A manifest object has these keys only:

- `schema_version`: optional, defaults to `0.1.0`; no other version accepted.
- `migration_id`: required ASCII `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`.
- `description`: optional string, at most 1024 characters.
- `up_statements`, `down_statements`: required nonempty arrays of SQL strings.
- `baseline_invariants`, `post_up_invariants`, `post_down_invariants`: optional arrays, default empty.

Invariant keys: required `id`, `query`, `expected`; optional `description` (512 characters). IDs use the migration ID pattern and must be unique across all lists. `expected` has exactly `type` and `value`: `integer` with signed 64-bit JSON integer (not bool), `text` with valid UTF-8 text, `null` with null, or `blob` with hexadecimal string. BLOB hex is canonicalized to lowercase without whitespace. Unknown keys, duplicate JSON keys, invalid UTF-8, unpaired surrogates, nonfinite numbers and all JSON floating-point values are rejected.

Example complete manifest:

```json
{
  "schema_version": "0.1.0",
  "migration_id": "rename_synthetic",
  "up_statements": ["UPDATE users SET name='Ada Example' WHERE id=1"],
  "down_statements": ["UPDATE users SET name='Ada' WHERE id=1"],
  "post_up_invariants": [
    {"id": "two", "query": "SELECT count(*) FROM users", "expected": {"type": "integer", "value": 2}}
  ]
}
```

Validation checks lexical policy, not SQL syntax or object existence. Execution can still fail.

## Transaction sequence

1. Validate source, fingerprint its main file, create bounded consistent backup.
2. Enable `PRAGMA foreign_keys=ON` outside transactions and verify the actual `PRAGMA foreign_keys` result is exactly `[(1,)]`. Inspect baseline and evaluate baseline invariants.
3. Verify FK enforcement, `BEGIN IMMEDIATE`, execute up entries individually with `Connection.execute`, inspect state and check FK/integrity, evaluate post-up invariants, `COMMIT`.
4. Verify FK enforcement, begin, execute down individually, inspect/check state, evaluate post-down invariants, commit.
5. Compare down to baseline. Stop on mismatch; a successful down commit is not described as a rollback.
6. Reapply up with the same checks and post-up invariants, commit; compare second up to first up.
7. Close working connections, remove only the owned workspace, fingerprint source again.

On transaction failure the engine explicitly attempts `ROLLBACK`, or observes an already ended SQLite transaction. `failure.transaction_rolled_back` records that the transaction ended after this attempt; `transaction_rollback_verified` separately records whether a fresh logical inspection equals the previous committed snapshot. No assertion is made that every SQLite error automatically rolls back earlier statements. A failed down transaction is checked against first-up state; failed reapplication is checked against down state.

## SQL policy

Migration starters: CREATE, ALTER, DROP, INSERT, UPDATE, DELETE, REPLACE, WITH. One statement per entry, with at most one trailing semicolon and optional comments. Transaction-control statements, ON CONFLICT ROLLBACK, ATTACH/DETACH, all PRAGMAs, VACUUM (including INTO), REINDEX, ANALYZE, extensions and temp/virtual/view/trigger objects are forbidden. CASE expressions with END are allowed. Generated/hidden objects are also rejected by post-statement inspection, including abbreviated generated-column syntax.

Authorizers independently deny transaction/savepoint, attachment, PRAGMA, virtual/view/trigger operations and direct temporary/non-main database access. The engine resets authorization context before every migration statement. Ordinary CREATE INDEX can authorize internal population of that same new index; explicit REINDEX remains forbidden. Main-database ALTER TABLE can inspect and rewrite only temporary-catalog SQL definitions needed by SQLite, without authorizing temporary table data, temporary DDL or a later statement. Working connections register no UDFs or extensions. SQL functions are restricted to `SAFE_FUNCTIONS` in `sql_guard.py` (ordinary scalar/aggregate text, numeric and byte functions); migration authorizers additionally allow named SQLite DDL string-rewrite helpers needed for ALTER TABLE. No application-defined, file-access or extension functions are offered.

Invariants must start with SELECT or WITH, remain read-only under an authorizer, and yield exactly one row and one column. Zero rows, multiple rows or multiple columns are `INVARIANT_SHAPE_ERROR`; typed scalar inequality is `INVARIANT_MISMATCH`. SQL failure, FK violation and enforcement failure have distinct codes. Query text and actual/expected cell values are never put in engine-generated diagnostics.

## Supported logical state

Ordinary rowid tables with explicit PKs, bounded INTEGER/TEXT/NULL/BLOB values, ordinary indexes and foreign keys. Actual PK tuples are checked for NULL and typed duplicates before rehearsal; declarations alone do not suffice. REAL values, invalid UTF-8 TEXT, generated/hidden/virtual objects, views, triggers, WITHOUT ROWID, expression indexes and custom index collations are unsupported. Internal objects such as `sqlite_sequence` and statistics tables are rejected; AUTOINCREMENT and databases retaining ANALYZE state are therefore outside this version. Empty databases are supported.

Catalogs retain exact stored table/index SQL plus columns, key order, FK metadata and ordinary index metadata. Built-in BINARY/NOCASE/RTRIM collations, descending and partial ordinary indexes are retained conservatively through exact SQL definitions. PK constraint index definitions are captured in exact table SQL. Row sorting uses typed key tuples with storage order NULL, INTEGER, TEXT, BLOB; integer numeric order, Unicode string order and byte order within each class. Unsupported NULL keys are rejected before sorting.

Equality is structured catalog equality plus typed-row equality, never digest equality alone. SQL definitions are compared byte-for-byte as Python strings without whitespace normalization. Physical pages, rowid allocation outside declared keys, freelists, journaling, user_version, application_id and arbitrary PRAGMA settings are not logical-state equivalence targets. Consequently results do not prove future behavior for every SQL query.

## Bounds

Configurable limits can only be lowered from hard ceilings, except VM steps can be raised from 50,000 to 500,000:

| Resource | Default / ceiling |
| --- | --- |
| Manifest UTF-8 bytes | 1,048,576 |
| Main file plus existing WAL, journal and SHM bytes | 16,777,216 |
| Backup/working main-file bytes | 16,777,216 |
| Tables | 32 |
| Total table rows per snapshot | 10,000 |
| Statements per up/down array | 64 |
| Statement/query UTF-8 bytes | 16,384 |
| Invariants per list | 32 |
| TEXT/BLOB bytes per cell | 65,536 |
| VM callbacks per migration batch, individual invariant or inspection | 50,000 / 500,000 |

Additional fixed caps: JSON nesting 12, decoded tree nodes including keys 4096, schema objects 256 including automatic indexes, columns/table 64, total indexes 128, FK-list entries/table 256, identifiers 128 UTF-8 bytes, serialized catalog 1 MiB, serialized logical snapshot 16 MiB, compact CLI output 4 MiB. Snapshot size includes escaped JSON and typed-value overhead, not only raw data. A table's row serialization is checked during streaming; aggregate row serialization is checked after each bounded table. The working connection also limits SQLite record/result length to 262,144 bytes, expression depth 64, compound SELECT terms 32 and variables 64. These may reject an otherwise small database or query.

Backup checks initial page count and then total/remaining pages on every one-page callback, limits cumulative busy/locked callbacks to 8 and all callbacks to `floor(max_baseline_bytes/page_size)+8`, with retry sleep zero. Source sidecars and destination size are checked during copying. A page may be copied before its callback, so an abnormal source can cause at most one page (64 KiB maximum) of pre-check copy overshoot. Working `max_page_count` is checked after copying. SQLite journaling and memory allocations are not comprehensively bounded by this limit.

VM budgets count progress-handler callbacks with interval 1. Inspection includes catalog queries, row scans and FK/integrity checks (`integrity_check(1)`); FK diagnostics stop at the first violating row. SQL RETURNING results are drained within the batch budget and discarded, with a row cap, not silently left half-executed. Backup has a separate budget because SQLite backup does not use the VM handler.

These are finite-input guardrails, **not a wall-clock, memory or arbitrary-code sandbox**. External process limits remain necessary. Trusted SQL/filesystem assumptions are required.

## Results, privacy and preservation

`RehearsalResult` exposes snapshots containing caller rows and exact SQL. `to_report_dict()` instead returns summaries: migration ID, status/success, source hashes and bytes, cleanup status, phases, invariant types/outcomes, snapshot counts/identifiers/logical hashes, comparison booleans/counts, rollback verification and sanitized failure. Error codes in `ErrorCode` are machine-readable. No raw SQL, SQLite error text, source/workspace paths or row/BLOB values appear in engine-generated diagnostics. Identifiers and fingerprints are retained, not anonymized; avoid sensitive identifiers. Caller-created diagnostic/value objects are not an output-sanitization API.

A matching main-file fingerprint proves byte preservation only for a quiescent fixture. It does not fingerprint WAL contents, stop external writers, establish absence of sidecar creation by SQLite, or provide recovery guarantees. Backup can include committed WAL data. Reports distinguish inability to verify and cleanup failure from success. A failed cleanup may leave the owned temporary workspace; it never authorizes removal of caller files.

CLI JSON uses one compact object on stdout. Human output uses fixed messages; argument errors do not echo supplied arguments. Exit 0: successful rehearsal/validation or demo expectations met; 1: failed rehearsal/demo/report budget; 2: invalid arguments, manifest/configuration, or outer setup failure. No persistent output files are written by the CLI.
