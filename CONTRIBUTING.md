# Contributing

Use synthetic fixtures only. Do not submit private client code, credentials, database contents or real personal data. Original contributions should be compatible with the MIT license.

Python 3.13+ and SQLite 3.35+ are required. No installation or network is needed for checks from the project directory:

```sh
python3 -m unittest discover -s tests -v
python3 -m sqlite_migration_rehearsal demo
python3 -m sqlite_migration_rehearsal demo --json
```

For optional packaging verification, prepare an isolated environment using real build tooling:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m sqlite_migration_rehearsal demo
```

That installation may need a package index for the pinned build backend; it is separate from offline tests/demo. Do not add runtime dependencies without a concrete need and an updated contract. Keep generated caches/build output out of contributions.

For behavior changes, include independent expected rows/catalog or a specific adversarial counterexample. Do not weaken FK checks, use executescript, normalize arbitrary SQL, or change a failed expectation to success merely to satisfy a test. Update both READMEs, the contract and unreleased changelog when changing public behavior or bounds. Keep diagnostics free of supplied SQL, cells, raw exceptions and paths. Do not turn this development utility into a live deployment workflow.

Use repository issues for non-sensitive bugs with a small synthetic reproducer and Python/SQLite versions. Security concerns use the private reporting route in SECURITY.md. General organizational contact is available at https://lofranotecnologia.com.br.
