# Changelog

## 0.1.0 — Unreleased

Development version; no release or deployment claim.

- Disposable consistent read-only-source SQLite backups with source fixture fingerprints.
- Engine-owned up/down/up transactions, explicit typed scalar invariants and conservative catalog/row comparisons.
- Bounded regular-file reads, JSON complexity, backup pages/retries, VM inspection/execution, schema metadata, values and report output.
- Distinct SQL, FK, enforcement, invariant, restoration and reapplication failures; explicit rollback verification and cleanup status.
- Synthetic offline demo with successful, lossy-down, failed-invariant and failed-SQL scenarios.
- Independent regression and adversarial tests, including destructive FK cascade detection and partial-backup cleanup.
- Allow SQLite's same-statement index population authorization while retaining the explicit REINDEX guard, with a regression for authorization reuse.
- Handle ALTER TABLE's action-specific metadata and internal temporary-catalog inspection without authorizing later temporary-database access.
- Reject invalid public hashing byte budgets before opening a source file.
- English/Portuguese documentation, precise supported-subset contract, packaging and read-only checks-only CI.

* Qualify actual linked SQLite 3.51.3+ and provide a checksum-pinned SQLite 3.54.1 runtime build; preserve journal and transaction semantics.
