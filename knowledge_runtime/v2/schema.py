from __future__ import annotations

from pathlib import Path

from .canonical import SchemaMigrator


def migration_directory() -> Path:
    return Path(__file__).with_name("migrations")


__all__ = ["SchemaMigrator", "migration_directory"]

