# Design and review rationale

## Preserve the useful narrow core

The public API remains centered on `MigrationManifest`, `RehearsalLimits`, `rehearse_migration`, `inspect_snapshot` and `compare_snapshots`, with explicit value objects and sanitized diagnostics. No runtime dependency or service is required. The working database is always a consistent SQLite backup, not a potentially torn filesystem copy, and the source connection is read-only.

The engine owns transaction boundaries and uses `Connection.execute` per entry. It never uses `executescript`, whose legacy transaction behavior can commit a pending transaction. Lexical checks produce early failures; independent authorizers enforce connection-level operation restrictions. Revalidation at the engine boundary prevents hand-constructed dataclasses bypassing parser checks. Cached statements are disabled on working connections so authorizer changes cannot reuse a previously prepared statement under a different policy.

SQL error rollback is separate from authored down SQL. Both are observable: transaction rollback attempts are followed by a fresh logical snapshot, whereas successful down SQL commits and then undergoes restoration comparison. Reapplication repeats the post-up invariants and compares against first-up state. RETURNING and CTE SELECT results are fully stepped under the VM budget rather than leaving deferred work outside it.

## Conservative state model

Exact stored SQL prevents incorrect normalization of quoted strings or identifiers. Structured metadata and typed rows supply understandable difference counts. Hashes are report fingerprints, not the equality oracle. PK row validation catches SQLite's nullable non-INTEGER PRIMARY KEY behavior; a lower-level real-row regression also checks duplicate detection independent of the declaration mechanism.

The supported subset deliberately excludes hidden state such as AUTOINCREMENT sequence tables, expression indexes, generated columns and WITHOUT ROWID. Supporting these later would require a new explicit contract and fixtures, not silently ignoring them. Built-in index collation and direction semantics are retained through exact definitions and validated index metadata.

## Resource accounting

File hashing has a byte ceiling and EOF probe. Manifest reads have a ceiling even if a file grows after stat; nesting is checked before recursive JSON decoding and tree complexity before serialization. Files are checked as regular on both path and descriptor. Trusted parent directories are still required; this is not race-proof filesystem isolation.

Backup has its own progress/page/busy/callback budget; a SQLite VM handler does not constrain backup. Copying one page per callback limits pre-check growth. Sidecars are checked before and during backup. VM handlers also cover inspection, where the original unbounded catalog scans and integrity checks otherwise permitted expensive work outside migration budgets. Row counts, identifiers, metadata, serialized states and CLI output have additional explicit ceilings.

SQLite native functions and filesystem calls can consume memory or time between callbacks. `max_page_count` does not cover every temporary/journal allocation. External process limits and trusted inputs remain necessary; no sandbox or production-recovery claim is made.

## Failure handling and privacy

Expected SQLite and filesystem errors become fixed-message diagnostics, without raw exceptions, SQL, cell contents or paths. Foreign-key enforcement is set and then queried before BEGIN, rather than trusting successful execution of a possibly mistyped PRAGMA. FK checks and integrity checks yield distinct codes; FK output is not collected. CLI argument errors are fixed-message as well.

Library snapshots deliberately retain data for inspection. Summary reports omit cells and definitions, but identifiers and digests can still be sensitive. Do not publish reports from private databases merely because JSON is escaped. If workspace removal fails, the result is unsuccessful and reports incomplete cleanup rather than hiding it.

## Regression evidence provided by tests

- Hand-authored users baseline and post-up catalog/rows, including SQL and typed values.
- Valid parent-column removal preserving child rows, plus the destructive parent rebuild that legitimately cascades away a child and must fail restoration.
- Lossy down retaining identical catalog but omitting Lin.
- SQL error after UPDATE and invariant failure after UPDATE, each checked against expected baseline rows and SQL after rollback.
- Direct authorizer attacks independent of lexical validation, including transaction control and quoted extension calls.
- Immediate/deferred FK violations, mistyped FK PRAGMA, shape/type invariant failures, CASE expressions, VM exhaustion and oversized result functions.
- Malformed/deep/duplicate-key JSON, strict scalar types, schema and file ceilings, nullable keys, real duplicate rows, unsupported objects, exact-SQL differences and distinct integrity failures.
- Backup busy/restart/growth callback simulations, partial-workspace cleanup, actual WAL backup, exclusive destination protection and bounded hashing.
- CLI sanitization with synthetic markers and cleanup-error reporting.

Tests are supplied for execution by contributors and CI; this document does not assert an unobserved successful run.

## Primary references

- [Python sqlite3 backup and transactions](https://docs.python.org/3.13/library/sqlite3.html)
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [foreign_keys](https://www.sqlite.org/pragma.html#pragma_foreign_keys)
- [foreign_key_check](https://www.sqlite.org/pragma.html#pragma_foreign_key_check)

These references motivate the narrow behavior implemented here; they are not claims of comprehensive SQLite standards compliance.
