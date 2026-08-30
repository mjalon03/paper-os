"""Ordered, transactional database migrations for the paper service."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .v001_initial import SQL as INITIAL_SCHEMA_SQL
from .v002_reading_states import SQL as READING_STATES_SQL
from .v003_reader_issues import SQL as READER_ISSUES_SQL
from .v004_annotations import SQL as ANNOTATIONS_SQL
from .v005_paper_knowledge import SQL as PAPER_KNOWLEDGE_SQL
from .v006_search_qa import SQL as SEARCH_QA_SQL
from .v007_taxonomy import SQL as TAXONOMY_SQL
from .v008_operations import SQL as OPERATIONS_SQL


@dataclass(frozen=True)
class Migration:
    """One immutable schema migration."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return sha256(self.sql.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration(1, "initial_schema", INITIAL_SCHEMA_SQL),
    Migration(2, "reading_states", READING_STATES_SQL),
    Migration(3, "reader_issues", READER_ISSUES_SQL),
    Migration(4, "annotations", ANNOTATIONS_SQL),
    Migration(5, "paper_knowledge", PAPER_KNOWLEDGE_SQL),
    Migration(6, "search_qa", SEARCH_QA_SQL),
    Migration(7, "adaptive_taxonomy", TAXONOMY_SQL),
    Migration(8, "operations", OPERATIONS_SQL),
)


def validate_migrations() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise RuntimeError("database migrations must have unique ascending versions")
    if versions and versions != list(range(1, versions[-1] + 1)):
        raise RuntimeError("database migration versions must be contiguous")
    if any(not migration.name or not migration.sql.strip() for migration in MIGRATIONS):
        raise RuntimeError("database migrations must have a name and SQL")


validate_migrations()
