"""Module entry point for `python3 -m sqlite_migration_rehearsal`."""

import sys

from sqlite_migration_rehearsal.cli import main

if __name__ == "__main__":
    sys.exit(main())
