# Security

Development version 0.1.0 is unreleased. No production-readiness, recovery guarantee, certification or response-time commitment is made.

Report vulnerabilities through GitHub private vulnerability reporting when available; otherwise request a private route through the official homepage:

https://github.com/martinlofranodeoliveira/sqlite-migration-rehearsal-lab/security/advisories/new

Do not put sensitive databases, SQL, identifiers, credentials or personal data into public issues. Prefer a minimal synthetic reproducer and describe the affected boundary. General contact: https://lofranotecnologia.com.br.

## Trust boundary

This utility accepts **trusted local SQLite files and trusted SQL**, not arbitrary hostile programs. Statement guards, authorizers, byte/page/row limits and VM budgets are defense in depth, not a security sandbox. SQLite native code and functions may consume resources between callbacks. Use external process CPU/memory/time limits and trusted finite filesystems for expensive inputs. No extensions or UDFs are loaded by the engine.

The source is opened read-only and migration statements run only on a new backup. External writers and concurrent path replacement are not controlled. Parent directories must be trusted. Main-file hashes establish preservation only for quiescent fixtures and do not identify WAL state. SQLite can manage source sidecars while reading WAL databases.

Returned library snapshots contain caller rows and catalog SQL. CLI summaries omit SQL/cells but retain identifiers and hashes, which are not anonymized. Treat both as potentially sensitive. Cleanup failure is reported and may leave an owned temporary workspace; follow host procedures for such directories. Do not use this tool for live recovery or production deployment.
