"""PostgreSQL-backed job queue and paper registry."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4, uuid5

from .migrations import MIGRATIONS

MIGRATION_ADVISORY_LOCK_ID = 806244902

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

PUBLIC_JOB_COLUMNS = """
id, trigger, status, requested_at, started_at, finished_at,
files_seen, files_imported, files_duplicate, files_invalid,
files_organized, files_processing_failed, files_reader_ready,
files_reader_failed, error_code
"""

READING_STATE_COLUMNS = """
paper_id, status, progress, anchor, article_fingerprint, favorite, priority,
created_at, updated_at, completed_at, revision
"""

READING_STATUSES = frozenset(
    {"unread", "reading", "completed", "on_hold", "rereading"}
)

READER_ISSUE_COLUMNS = """
id, paper_id, kind, description, anchor, article_fingerprint, source,
external_key, status, created_at, updated_at, resolved_at, revision
"""

ANNOTATION_COLUMNS = """
id, paper_id, kind, anchor, body, category, question_status,
article_fingerprint, ai_share_allowed, created_at, updated_at, trashed_at, revision
"""

READER_ISSUE_KINDS = frozenset(
    {
        "extraction_text",
        "sentence_break",
        "section_structure",
        "figure_crop",
        "table",
        "metadata",
        "other",
    }
)
READER_ISSUE_SOURCES = frozenset({"user", "system"})
READER_ISSUE_STATUSES = frozenset({"open", "resolved", "dismissed"})
ANNOTATION_KINDS = frozenset({"highlight", "note", "question"})
QUESTION_STATUSES = frozenset({"open", "answered", "dismissed"})
SELECTOR_VERSION = 1
_SYSTEM_ISSUE_NAMESPACE = UUID("b274a37f-6a24-5d31-99f6-69d14bc18066")
_UNSET = object()


class MigrationError(RuntimeError):
    """Raised when migration history does not match this service build."""


class PaperNotFound(LookupError):
    """Raised when a write targets a paper that does not exist."""


class ReadingStateConflict(RuntimeError):
    """Raised when expected_revision is not the current revision."""

    def __init__(self, current_state: dict[str, Any] | None):
        super().__init__("reading state revision conflict")
        self.current_state = current_state


class ReaderIssueNotFound(LookupError):
    """Raised when an issue id does not exist."""


class ReaderIssueConflict(RuntimeError):
    """Raised when an issue revision or client id conflicts."""

    def __init__(self, current_issue: dict[str, Any] | None):
        super().__init__("reader issue revision conflict")
        self.current_issue = current_issue


class AnnotationNotFound(LookupError):
    """Raised when an annotation id does not exist."""


class AnnotationConflict(RuntimeError):
    """Raised when an annotation revision or client id conflicts."""

    def __init__(self, current_annotation: dict[str, Any] | None):
        super().__init__("annotation revision conflict")
        self.current_annotation = current_annotation


class AnnotationTrashed(RuntimeError):
    """Raised when an active-only write targets a trashed annotation."""


def database_config() -> dict[str, str]:
    name = os.environ.get("PAPER_DB_NAME", "paper_os")
    user = os.environ.get("PAPER_DB_USER", "paper_os")
    password = os.environ.get("PAPER_DB_PASSWORD")
    host = os.environ.get("PAPER_DB_HOST", "postgres")
    port = os.environ.get("PAPER_DB_PORT", "5432")
    if not password:
        raise RuntimeError("PAPER_DB_PASSWORD must be set")
    return {
        "dbname": name,
        "user": user,
        "password": password,
        "host": host,
        "port": port,
    }


@contextmanager
def connection() -> Iterator[Any]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(**database_config(), row_factory=dict_row) as database:
        yield database


def apply_migrations(database: Any) -> None:
    """Apply pending migrations inside the caller's database transaction."""

    database.execute(
        "SELECT pg_advisory_xact_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,)
    )
    database.execute(MIGRATION_TABLE_SQL)
    rows = database.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(row["version"]): row for row in rows}
    known_versions = {migration.version for migration in MIGRATIONS}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise MigrationError(
            f"database has unknown migration versions: {', '.join(map(str, unknown))}"
        )
    expected_prefix = [migration.version for migration in MIGRATIONS[: len(applied)]]
    if sorted(applied) != expected_prefix:
        raise MigrationError("database migration history is not an ordered prefix")

    for migration in MIGRATIONS:
        previous = applied.get(migration.version)
        if previous:
            if (
                previous["name"] != migration.name
                or previous["checksum"] != migration.checksum
            ):
                raise MigrationError(
                    f"migration {migration.version} history does not match this build"
                )
            continue
        database.execute(migration.sql)
        database.execute(
            """INSERT INTO schema_migrations (version, name, checksum)
               VALUES (%s, %s, %s)""",
            (migration.version, migration.name, migration.checksum),
        )


def ensure_schema() -> None:
    with connection() as database:
        apply_migrations(database)


def queue_sync(trigger: str, schedule_key: str | None = None) -> dict[str, Any]:
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("invalid sync trigger")
    with connection() as database:
        database.execute("SELECT pg_advisory_xact_lock(806244901)")
        if schedule_key:
            existing = database.execute(
                f"SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs WHERE schedule_key = %s",
                (schedule_key,),
            ).fetchone()
            if existing:
                return dict(existing)
        active = database.execute(
            f"""SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs
                WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1"""
        ).fetchone()
        if active:
            return dict(active)
        created = database.execute(
            f"""INSERT INTO sync_jobs (trigger, schedule_key, status)
                VALUES (%s, %s, 'queued') RETURNING {PUBLIC_JOB_COLUMNS}""",
            (trigger, schedule_key),
        ).fetchone()
        return dict(created)


def latest_sync() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            f"SELECT {PUBLIC_JOB_COLUMNS} FROM sync_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def recover_interrupted_sync() -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'queued', started_at = NULL,
               error_code = NULL WHERE status = 'running'"""
        )


def claim_sync() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE sync_jobs SET status = 'running', started_at = NOW()
               WHERE id = (
                   SELECT id FROM sync_jobs WHERE status = 'queued'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, trigger, status"""
        ).fetchone()
        return dict(row) if row else None


def finish_sync(job_id: int, stats: Any) -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'succeeded', finished_at = NOW(),
               files_seen = %s, files_imported = %s, files_duplicate = %s,
               files_invalid = %s, files_organized = %s,
               files_processing_failed = %s, files_reader_ready = %s,
               files_reader_failed = %s, error_code = NULL WHERE id = %s""",
            (
                stats.seen,
                stats.imported,
                stats.duplicate,
                stats.invalid,
                getattr(stats, "organized", 0),
                getattr(stats, "processing_failed", 0),
                getattr(stats, "reader_ready", 0),
                getattr(stats, "reader_failed", 0),
                job_id,
            ),
        )


def fail_sync(job_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE sync_jobs SET status = 'failed', finished_at = NOW(),
               error_code = %s WHERE id = %s""",
            (error_code[:80], job_id),
        )


def register_paper(
    sha256: str,
    size_bytes: int,
    source_path: str,
    stored_path: str,
    job_id: int,
) -> bool:
    with connection() as database:
        row = database.execute(
            """INSERT INTO papers
               (sha256, size_bytes, source_path, stored_path, first_job_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (sha256) DO NOTHING RETURNING id""",
            (sha256, size_bytes, source_path, stored_path, job_id),
        ).fetchone()
        return row is not None


def recover_processing_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'pending',
               processing_error = 'worker_interrupted'
               WHERE processing_status = 'processing'"""
        )


def retry_failed_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'pending'
               WHERE processing_status = 'failed' AND processing_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_processing() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE papers SET processing_status = 'processing',
               processing_attempts = processing_attempts + 1,
               processing_error = NULL
               WHERE id = (
                   SELECT id FROM papers WHERE processing_status = 'pending'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, size_bytes, source_path, stored_path"""
        ).fetchone()
        return dict(row) if row else None


def complete_paper_processing(
    paper_id: int,
    metadata: dict[str, Any],
    drive_path: str,
) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'organized', title = %s,
               authors = %s::jsonb, publication_year = %s, doi = %s,
               category = %s, paper_type = %s, confidence = %s,
               drive_path = %s, metadata_json = %s::jsonb,
               processed_at = NOW(), processing_error = NULL WHERE id = %s""",
            (
                metadata["title"],
                json.dumps(metadata["authors"], ensure_ascii=False),
                metadata["year"],
                metadata["doi"],
                metadata["category"],
                metadata["paper_type"],
                metadata["confidence"],
                drive_path,
                json.dumps(metadata, ensure_ascii=False),
                paper_id,
            ),
        )


def fail_paper_processing(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET processing_status = 'failed',
               processing_error = %s WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def recover_reader_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'pending',
               reader_error = 'worker_interrupted'
               WHERE reader_status = 'processing'"""
        )


def retry_failed_reader_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'pending'
               WHERE reader_status = 'failed' AND reader_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_reader() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """UPDATE papers SET reader_status = 'processing',
               reader_attempts = reader_attempts + 1, reader_error = NULL
               WHERE id = (
                   SELECT id FROM papers
                   WHERE processing_status = 'organized' AND reader_status = 'pending'
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, size_bytes, stored_path, title, authors,
                         publication_year, doi, category, paper_type"""
        ).fetchone()
        return dict(row) if row else None


def complete_reader_paper(paper_id: int, manifest_path: str, version: int) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'ready', reader_version = %s,
               reader_manifest_path = %s, reader_generated_at = NOW(),
               reader_error = NULL WHERE id = %s""",
            (version, manifest_path, paper_id),
        )


def fail_reader_paper(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_status = 'failed', reader_error = %s
               WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def recover_reader_v2_papers() -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'pending',
               reader_v2_error = 'worker_interrupted'
               WHERE reader_v2_status = 'processing'"""
        )


def retry_failed_reader_v2_papers(max_attempts: int = 3) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'pending'
               WHERE reader_v2_status = 'failed' AND reader_v2_attempts < %s""",
            (max_attempts,),
        )


def claim_paper_for_reader_v2(allowed_ids: list[int] | None) -> dict[str, Any] | None:
    with connection() as database:
        parameters: tuple[Any, ...] = ()
        allowed = ""
        if allowed_ids is not None:
            if not allowed_ids:
                return None
            allowed = " AND id = ANY(%s)"
            parameters = (allowed_ids,)
        row = database.execute(
            f"""UPDATE papers SET reader_v2_status = 'processing',
               reader_v2_attempts = reader_v2_attempts + 1,
               reader_v2_error = NULL
               WHERE id = (
                   SELECT id FROM papers
                   WHERE processing_status = 'organized'
                     AND reader_v2_status = 'pending'{allowed}
                   ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               RETURNING id, sha256, size_bytes, stored_path, title, authors,
                         publication_year, doi, category, paper_type""",
            parameters,
        ).fetchone()
        return dict(row) if row else None


def complete_reader_v2_paper(paper_id: int, manifest_path: str, source: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'ready',
               reader_v2_source = %s, reader_v2_manifest_path = %s,
               reader_v2_generated_at = NOW(), reader_v2_error = NULL
               WHERE id = %s""",
            (source, manifest_path, paper_id),
        )


def fail_reader_v2_paper(paper_id: int, error_code: str) -> None:
    with connection() as database:
        database.execute(
            """UPDATE papers SET reader_v2_status = 'failed', reader_v2_error = %s
               WHERE id = %s""",
            (error_code[:80], paper_id),
        )


def list_reader_papers() -> list[dict[str, Any]]:
    with connection() as database:
        rows = database.execute(
            """SELECT p.id, p.title, p.authors, p.publication_year,
                      p.category, p.paper_type, p.reader_status,
                      p.reader_generated_at, p.reader_v2_status,
                      p.reader_v2_generated_at,
                      COALESCE(rs.status, 'unread') AS reading_status,
                      COALESCE(rs.progress, 0) AS reading_progress,
                      COALESCE(rs.favorite, FALSE) AS favorite,
                      COALESCE(rs.priority, 0) AS priority,
                      rs.updated_at AS reading_updated_at
               FROM papers p
               LEFT JOIN reading_states rs ON rs.paper_id = p.id
               WHERE p.processing_status = 'organized'
               ORDER BY COALESCE(rs.favorite, FALSE) DESC,
                        COALESCE(rs.priority, 0) DESC,
                        p.imported_at DESC, p.id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]


def get_reader_paper(paper_id: int) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """SELECT id, title, authors, publication_year, doi, category,
                      paper_type, reader_status, reader_manifest_path,
                      reader_generated_at, stored_path
               FROM papers WHERE id = %s AND processing_status = 'organized'""",
            (paper_id,),
        ).fetchone()
        return dict(row) if row else None


def list_reader_v2_ready_ids() -> list[int]:
    """Return only IDs needed for artifact maintenance jobs."""

    with connection() as database:
        rows = database.execute(
            """SELECT id FROM papers
               WHERE processing_status = 'organized'
                 AND reader_v2_status = 'ready'
               ORDER BY id"""
        ).fetchall()
        return [int(row["id"]) for row in rows]


def get_reading_state(paper_id: int) -> dict[str, Any] | None:
    """Return the persisted reading state, or None before its first write."""

    _validate_positive_int("paper_id", paper_id)
    with connection() as database:
        row = database.execute(
            f"SELECT {READING_STATE_COLUMNS} FROM reading_states WHERE paper_id = %s",
            (paper_id,),
        ).fetchone()
        return dict(row) if row else None


def list_reading_states(paper_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Return reading states, optionally limited to an explicit paper set."""

    parameters: tuple[Any, ...] = ()
    where = ""
    if paper_ids is not None:
        if not paper_ids:
            return []
        for paper_id in paper_ids:
            _validate_positive_int("paper_id", paper_id)
        where = " WHERE paper_id = ANY(%s)"
        parameters = (paper_ids,)
    with connection() as database:
        rows = database.execute(
            f"SELECT {READING_STATE_COLUMNS} FROM reading_states{where} "
            "ORDER BY updated_at DESC, paper_id DESC",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]


def update_reading_state(
    paper_id: int,
    *,
    expected_revision: int,
    status: Any = _UNSET,
    progress: Any = _UNSET,
    anchor: Any = _UNSET,
    article_fingerprint: Any = _UNSET,
    favorite: Any = _UNSET,
    priority: Any = _UNSET,
) -> dict[str, Any]:
    """Create or patch a reading state using optimistic revision control.

    A paper without state has logical revision 0. Its first write must therefore
    use ``expected_revision=0``. Every successful write increments ``revision``.
    """

    values = _validate_reading_state_update(
        paper_id=paper_id,
        expected_revision=expected_revision,
        status=status,
        progress=progress,
        anchor=anchor,
        article_fingerprint=article_fingerprint,
        favorite=favorite,
        priority=priority,
    )
    with connection() as database:
        return _update_reading_state(
            database,
            paper_id=paper_id,
            expected_revision=expected_revision,
            values=values,
        )


def _update_reading_state(
    database: Any,
    *,
    paper_id: int,
    expected_revision: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    current_row = database.execute(
        f"""SELECT {READING_STATE_COLUMNS} FROM reading_states
            WHERE paper_id = %s FOR UPDATE""",
        (paper_id,),
    ).fetchone()
    current = dict(current_row) if current_row else None

    if current is None:
        paper = database.execute(
            "SELECT id FROM papers WHERE id = %s", (paper_id,)
        ).fetchone()
        if not paper:
            raise PaperNotFound(f"paper {paper_id} does not exist")
        if expected_revision != 0:
            raise ReadingStateConflict(None)

        state = {
            "status": "unread",
            "progress": 0.0,
            "anchor": None,
            "article_fingerprint": None,
            "favorite": False,
            "priority": 0,
        }
        state.update(values)
        _validate_merged_reading_anchor(state)
        inserted = database.execute(
            f"""INSERT INTO reading_states
                (paper_id, status, progress, anchor, article_fingerprint,
                 favorite, priority, completed_at, revision)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s,
                        CASE WHEN %s = 'completed' THEN NOW() ELSE NULL END, 1)
                ON CONFLICT (paper_id) DO NOTHING
                RETURNING {READING_STATE_COLUMNS}""",
            (
                paper_id,
                state["status"],
                state["progress"],
                _json_value(state["anchor"]),
                state["article_fingerprint"],
                state["favorite"],
                state["priority"],
                state["status"],
            ),
        ).fetchone()
        if inserted:
            return dict(inserted)
        concurrent = database.execute(
            f"SELECT {READING_STATE_COLUMNS} FROM reading_states WHERE paper_id = %s",
            (paper_id,),
        ).fetchone()
        raise ReadingStateConflict(dict(concurrent) if concurrent else None)

    if int(current["revision"]) != expected_revision:
        raise ReadingStateConflict(current)

    state = {
        "status": current["status"],
        "progress": current["progress"],
        "anchor": current["anchor"],
        "article_fingerprint": current["article_fingerprint"],
        "favorite": current["favorite"],
        "priority": current["priority"],
    }
    state.update(values)
    _validate_merged_reading_anchor(state)
    updated = database.execute(
        f"""UPDATE reading_states
            SET status = %s, progress = %s, anchor = %s::jsonb,
                article_fingerprint = %s, favorite = %s, priority = %s,
                updated_at = NOW(),
                completed_at = CASE
                    WHEN %s = 'completed' THEN COALESCE(completed_at, NOW())
                    ELSE NULL
                END,
                revision = revision + 1
            WHERE paper_id = %s AND revision = %s
            RETURNING {READING_STATE_COLUMNS}""",
        (
            state["status"],
            state["progress"],
            _json_value(state["anchor"]),
            state["article_fingerprint"],
            state["favorite"],
            state["priority"],
            state["status"],
            paper_id,
            expected_revision,
        ),
    ).fetchone()
    if not updated:
        concurrent = database.execute(
            f"SELECT {READING_STATE_COLUMNS} FROM reading_states WHERE paper_id = %s",
            (paper_id,),
        ).fetchone()
        raise ReadingStateConflict(dict(concurrent) if concurrent else None)
    return dict(updated)


def _validate_reading_state_update(**values: Any) -> dict[str, Any]:
    paper_id = values.pop("paper_id")
    expected_revision = values.pop("expected_revision")
    _validate_positive_int("paper_id", paper_id)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")

    supplied = {key: value for key, value in values.items() if value is not _UNSET}
    if not supplied:
        raise ValueError("at least one reading state field must be supplied")

    if "status" in supplied and supplied["status"] not in READING_STATUSES:
        raise ValueError("invalid reading status")
    if "progress" in supplied:
        value = supplied["progress"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("progress must be a finite number from 0 to 1")
        supplied["progress"] = float(value)
    if "article_fingerprint" in supplied:
        value = supplied["article_fingerprint"]
        if value is not None and (
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", value)
        ):
            raise ValueError("article_fingerprint must be a lowercase SHA-256")
    if "anchor" in supplied:
        value = supplied["anchor"]
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError("anchor must be an object or null")
            embedded_fingerprint = _validate_fingerprint(
                value.get("article_fingerprint")
            )
            supplied_fingerprint = supplied.get("article_fingerprint", _UNSET)
            if supplied_fingerprint is None:
                raise ValueError(
                    "a non-null anchor cannot have a null article_fingerprint"
                )
            if (
                supplied_fingerprint is not _UNSET
                and supplied_fingerprint != embedded_fingerprint
            ):
                raise ValueError("anchor article_fingerprint does not match")
            supplied["anchor"] = _validate_stable_anchor(
                value,
                article_fingerprint=embedded_fingerprint,
                require_selection=False,
            )
            if expected_revision == 0 and supplied_fingerprint is _UNSET:
                raise ValueError(
                    "the first anchored state requires article_fingerprint"
                )
    if "favorite" in supplied and not isinstance(supplied["favorite"], bool):
        raise ValueError("favorite must be a boolean")
    if "priority" in supplied:
        value = supplied["priority"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 5
        ):
            raise ValueError("priority must be an integer from 0 to 5")
    return supplied


def _validate_merged_reading_anchor(state: dict[str, Any]) -> None:
    """Validate an existing-or-new resume anchor before its write statement."""

    anchor = state["anchor"]
    if anchor is None:
        return
    fingerprint = _validate_fingerprint(state["article_fingerprint"])
    state["anchor"] = _validate_stable_anchor(
        anchor,
        article_fingerprint=fingerprint,
        require_selection=False,
    )


def _validate_positive_int(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _json_value(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def list_reader_issues(
    paper_id: int,
    *,
    status: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """List durable quality issues for one paper."""

    _validate_positive_int("paper_id", paper_id)
    if status is not None and status not in READER_ISSUE_STATUSES:
        raise ValueError("invalid reader issue status")
    if source is not None and source not in READER_ISSUE_SOURCES:
        raise ValueError("invalid reader issue source")
    conditions = ["paper_id = %s"]
    parameters: list[Any] = [paper_id]
    if status is not None:
        conditions.append("status = %s")
        parameters.append(status)
    if source is not None:
        conditions.append("source = %s")
        parameters.append(source)
    with connection() as database:
        rows = database.execute(
            f"""SELECT {READER_ISSUE_COLUMNS} FROM reader_issues
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                         updated_at DESC, id""",
            tuple(parameters),
        ).fetchall()
        return [dict(row) for row in rows]


def create_reader_issue(
    paper_id: int,
    *,
    kind: str,
    anchor: dict[str, Any],
    article_fingerprint: str,
    description: str | None = None,
    source: str = "user",
    external_key: str | None = None,
    issue_id: str | UUID | None = None,
) -> dict[str, Any]:
    """Create a user/system issue; exact client retries are idempotent."""

    values = _validate_reader_issue_create(
        paper_id=paper_id,
        kind=kind,
        anchor=anchor,
        article_fingerprint=article_fingerprint,
        description=description,
        source=source,
        external_key=external_key,
        issue_id=issue_id,
    )
    with connection() as database:
        paper = database.execute(
            "SELECT id FROM papers WHERE id = %s", (paper_id,)
        ).fetchone()
        if not paper:
            raise PaperNotFound(f"paper {paper_id} does not exist")
        return _insert_reader_issue(database, values)


def _insert_reader_issue(
    database: Any, values: dict[str, Any]
) -> dict[str, Any]:
    inserted = database.execute(
        f"""INSERT INTO reader_issues
            (id, paper_id, kind, description, anchor, article_fingerprint,
             source, external_key, status, revision)
            VALUES (%s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s, 'open', 1)
            ON CONFLICT DO NOTHING
            RETURNING {READER_ISSUE_COLUMNS}""",
        (
            values["id"],
            values["paper_id"],
            values["kind"],
            values["description"],
            _json_value(values["anchor"]),
            values["article_fingerprint"],
            values["source"],
            values["external_key"],
        ),
    ).fetchone()
    if inserted:
        return dict(inserted)

    current_row = database.execute(
        f"""SELECT {READER_ISSUE_COLUMNS} FROM reader_issues
            WHERE id = %s::uuid
               OR (paper_id = %s AND external_key = %s)
            ORDER BY CASE WHEN id = %s::uuid THEN 0 ELSE 1 END
            LIMIT 1""",
        (
            values["id"],
            values["paper_id"],
            values["external_key"],
            values["id"],
        ),
    ).fetchone()
    current = dict(current_row) if current_row else None
    if current and _reader_issue_create_matches(current, values):
        return current
    raise ReaderIssueConflict(current)


def update_reader_issue(
    issue_id: str | UUID,
    *,
    expected_revision: int,
    description: Any = _UNSET,
    status: Any = _UNSET,
    anchor: Any = _UNSET,
    article_fingerprint: Any = _UNSET,
) -> dict[str, Any]:
    """Patch an issue without deleting its history."""

    normalized_id, values = _validate_reader_issue_update(
        issue_id=issue_id,
        expected_revision=expected_revision,
        description=description,
        status=status,
        anchor=anchor,
        article_fingerprint=article_fingerprint,
    )
    with connection() as database:
        return _update_reader_issue(
            database,
            issue_id=normalized_id,
            expected_revision=expected_revision,
            values=values,
        )


def _update_reader_issue(
    database: Any,
    *,
    issue_id: str,
    expected_revision: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    current_row = database.execute(
        f"""SELECT {READER_ISSUE_COLUMNS} FROM reader_issues
            WHERE id = %s::uuid FOR UPDATE""",
        (issue_id,),
    ).fetchone()
    if not current_row:
        raise ReaderIssueNotFound(f"reader issue {issue_id} does not exist")
    current = dict(current_row)
    if int(current["revision"]) != expected_revision:
        raise ReaderIssueConflict(current)

    state = {
        "description": current["description"],
        "status": current["status"],
        "anchor": current["anchor"],
        "article_fingerprint": str(current["article_fingerprint"]),
    }
    state.update(values)
    if state["anchor"]["article_fingerprint"] != state["article_fingerprint"]:
        raise ValueError("anchor article_fingerprint does not match the issue")
    updated = database.execute(
        f"""UPDATE reader_issues
            SET description = %s, status = %s, anchor = %s::jsonb,
                article_fingerprint = %s, updated_at = NOW(),
                resolved_at = CASE
                    WHEN %s = 'open' THEN NULL
                    ELSE COALESCE(resolved_at, NOW())
                END,
                revision = revision + 1
            WHERE id = %s::uuid AND revision = %s
            RETURNING {READER_ISSUE_COLUMNS}""",
        (
            state["description"],
            state["status"],
            _json_value(state["anchor"]),
            state["article_fingerprint"],
            state["status"],
            issue_id,
            expected_revision,
        ),
    ).fetchone()
    if not updated:
        concurrent = database.execute(
            f"SELECT {READER_ISSUE_COLUMNS} FROM reader_issues WHERE id = %s::uuid",
            (issue_id,),
        ).fetchone()
        raise ReaderIssueConflict(dict(concurrent) if concurrent else None)
    return dict(updated)


def import_article_warnings(
    paper_id: int,
    *,
    warnings: list[str],
    article_fingerprint: str,
) -> list[dict[str, Any]]:
    """Import extractor warnings using a stable per-paper external key.

    Re-importing the same normalized warning returns its existing issue and
    never reopens, remaps, or overwrites a human-reviewed status.
    """

    _validate_positive_int("paper_id", paper_id)
    fingerprint = _validate_fingerprint(article_fingerprint)
    normalized_warnings = _validate_article_warnings(warnings)
    issue_values = [
        _system_warning_values(paper_id, warning, fingerprint)
        for warning in normalized_warnings
    ]
    with connection() as database:
        paper = database.execute(
            "SELECT id FROM papers WHERE id = %s", (paper_id,)
        ).fetchone()
        if not paper:
            raise PaperNotFound(f"paper {paper_id} does not exist")
        return [_insert_reader_issue(database, values) for values in issue_values]


def list_annotations(
    paper_id: int,
    *,
    kind: str | None = None,
    include_trashed: bool = False,
) -> list[dict[str, Any]]:
    """List a paper's active annotations, with an explicit trash opt-in."""

    _validate_positive_int("paper_id", paper_id)
    if kind is not None and kind not in ANNOTATION_KINDS:
        raise ValueError("invalid annotation kind")
    if not isinstance(include_trashed, bool):
        raise ValueError("include_trashed must be a boolean")
    conditions = ["paper_id = %s"]
    parameters: list[Any] = [paper_id]
    if kind is not None:
        conditions.append("kind = %s")
        parameters.append(kind)
    if not include_trashed:
        conditions.append("trashed_at IS NULL")
    with connection() as database:
        rows = database.execute(
            f"""SELECT {ANNOTATION_COLUMNS} FROM annotations
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC, id""",
            tuple(parameters),
        ).fetchall()
        return [dict(row) for row in rows]


def create_annotation(
    annotation_id: str | UUID,
    paper_id: int,
    *,
    kind: str,
    anchor: dict[str, Any],
    article_fingerprint: str,
    body: str | None = None,
    category: str | None = None,
    question_status: str | None = None,
    ai_share_allowed: bool = False,
    expected_revision: int = 0,
) -> dict[str, Any]:
    """Create a client-identified annotation at logical revision zero."""

    values = _validate_annotation_create(
        annotation_id=annotation_id,
        paper_id=paper_id,
        kind=kind,
        anchor=anchor,
        article_fingerprint=article_fingerprint,
        body=body,
        category=category,
        question_status=question_status,
        ai_share_allowed=ai_share_allowed,
        expected_revision=expected_revision,
    )
    with connection() as database:
        paper = database.execute(
            "SELECT id FROM papers WHERE id = %s", (paper_id,)
        ).fetchone()
        if not paper:
            raise PaperNotFound(f"paper {paper_id} does not exist")
        return _insert_annotation(database, values)


def _insert_annotation(database: Any, values: dict[str, Any]) -> dict[str, Any]:
    inserted = database.execute(
        f"""INSERT INTO annotations
            (id, paper_id, kind, anchor, body, category, question_status,
             article_fingerprint, ai_share_allowed, revision)
            VALUES (%s::uuid, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (id) DO NOTHING
            RETURNING {ANNOTATION_COLUMNS}""",
        (
            values["id"],
            values["paper_id"],
            values["kind"],
            _json_value(values["anchor"]),
            values["body"],
            values["category"],
            values["question_status"],
            values["article_fingerprint"],
            values["ai_share_allowed"],
        ),
    ).fetchone()
    if inserted:
        return dict(inserted)
    current_row = database.execute(
        f"SELECT {ANNOTATION_COLUMNS} FROM annotations WHERE id = %s::uuid",
        (values["id"],),
    ).fetchone()
    current = dict(current_row) if current_row else None
    if current and _annotation_create_matches(current, values):
        return current
    raise AnnotationConflict(current)


def update_annotation(
    annotation_id: str | UUID,
    *,
    expected_revision: int,
    body: Any = _UNSET,
    category: Any = _UNSET,
    question_status: Any = _UNSET,
    anchor: Any = _UNSET,
    article_fingerprint: Any = _UNSET,
    ai_share_allowed: Any = _UNSET,
) -> dict[str, Any]:
    """Patch an active annotation with explicit optimistic locking."""

    normalized_id, values = _validate_annotation_update(
        annotation_id=annotation_id,
        expected_revision=expected_revision,
        body=body,
        category=category,
        question_status=question_status,
        anchor=anchor,
        article_fingerprint=article_fingerprint,
        ai_share_allowed=ai_share_allowed,
    )
    with connection() as database:
        return _update_annotation(
            database,
            annotation_id=normalized_id,
            expected_revision=expected_revision,
            values=values,
        )


def _update_annotation(
    database: Any,
    *,
    annotation_id: str,
    expected_revision: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    current_row = database.execute(
        f"""SELECT {ANNOTATION_COLUMNS} FROM annotations
            WHERE id = %s::uuid FOR UPDATE""",
        (annotation_id,),
    ).fetchone()
    if not current_row:
        raise AnnotationNotFound(f"annotation {annotation_id} does not exist")
    current = dict(current_row)
    if int(current["revision"]) != expected_revision:
        raise AnnotationConflict(current)
    if current["trashed_at"] is not None:
        raise AnnotationTrashed(f"annotation {annotation_id} is trashed")

    state = {
        "anchor": current["anchor"],
        "body": current["body"],
        "category": current["category"],
        "question_status": current["question_status"],
        "article_fingerprint": str(current["article_fingerprint"]),
        "ai_share_allowed": current["ai_share_allowed"],
    }
    state.update(values)
    if current["kind"] == "question":
        if state["question_status"] not in QUESTION_STATUSES:
            raise ValueError("question annotations require a question_status")
    elif state["question_status"] is not None:
        raise ValueError("only question annotations have a question_status")
    if state["anchor"]["article_fingerprint"] != state["article_fingerprint"]:
        raise ValueError("anchor article_fingerprint does not match the annotation")

    updated = database.execute(
        f"""UPDATE annotations
            SET anchor = %s::jsonb, body = %s, category = %s,
                question_status = %s, article_fingerprint = %s,
                ai_share_allowed = %s,
                updated_at = NOW(), revision = revision + 1
            WHERE id = %s::uuid AND revision = %s AND trashed_at IS NULL
            RETURNING {ANNOTATION_COLUMNS}""",
        (
            _json_value(state["anchor"]),
            state["body"],
            state["category"],
            state["question_status"],
            state["article_fingerprint"],
            state["ai_share_allowed"],
            annotation_id,
            expected_revision,
        ),
    ).fetchone()
    if not updated:
        concurrent = database.execute(
            f"SELECT {ANNOTATION_COLUMNS} FROM annotations WHERE id = %s::uuid",
            (annotation_id,),
        ).fetchone()
        raise AnnotationConflict(dict(concurrent) if concurrent else None)
    return dict(updated)


def trash_annotation(
    annotation_id: str | UUID, *, expected_revision: int
) -> dict[str, Any]:
    """Soft-delete an annotation while retaining its evidence and history."""

    normalized_id = _validate_uuid("annotation_id", annotation_id)
    _validate_existing_revision(expected_revision)
    with connection() as database:
        current_row = database.execute(
            f"""SELECT {ANNOTATION_COLUMNS} FROM annotations
                WHERE id = %s::uuid FOR UPDATE""",
            (normalized_id,),
        ).fetchone()
        if not current_row:
            raise AnnotationNotFound(f"annotation {normalized_id} does not exist")
        current = dict(current_row)
        if int(current["revision"]) != expected_revision:
            raise AnnotationConflict(current)
        if current["trashed_at"] is not None:
            return current
        updated = database.execute(
            f"""UPDATE annotations
                SET trashed_at = NOW(), updated_at = NOW(), revision = revision + 1
                WHERE id = %s::uuid AND revision = %s AND trashed_at IS NULL
                RETURNING {ANNOTATION_COLUMNS}""",
            (normalized_id, expected_revision),
        ).fetchone()
        if not updated:
            concurrent = database.execute(
                f"SELECT {ANNOTATION_COLUMNS} FROM annotations WHERE id = %s::uuid",
                (normalized_id,),
            ).fetchone()
            raise AnnotationConflict(dict(concurrent) if concurrent else None)
        return dict(updated)


def _validate_reader_issue_create(**values: Any) -> dict[str, Any]:
    paper_id = values["paper_id"]
    _validate_positive_int("paper_id", paper_id)
    kind = values["kind"]
    if kind not in READER_ISSUE_KINDS:
        raise ValueError("invalid reader issue kind")
    source = values["source"]
    if source not in READER_ISSUE_SOURCES:
        raise ValueError("invalid reader issue source")
    external_key = _validate_optional_text(
        "external_key", values["external_key"], maximum=200, single_line=True
    )
    if source == "system" and external_key is None:
        raise ValueError("system issues require an external_key")
    if source == "user" and external_key is not None:
        raise ValueError("user issues cannot have an external_key")
    fingerprint = _validate_fingerprint(values["article_fingerprint"])
    anchor = _validate_stable_anchor(
        values["anchor"], article_fingerprint=fingerprint, require_selection=False
    )
    issue_id = (
        str(uuid4())
        if values["issue_id"] is None
        else _validate_uuid("issue_id", values["issue_id"])
    )
    return {
        "id": issue_id,
        "paper_id": paper_id,
        "kind": kind,
        "description": _validate_optional_text(
            "description", values["description"], maximum=4000
        ),
        "anchor": anchor,
        "article_fingerprint": fingerprint,
        "source": source,
        "external_key": external_key,
    }


def _validate_reader_issue_update(**values: Any) -> tuple[str, dict[str, Any]]:
    issue_id = _validate_uuid("issue_id", values.pop("issue_id"))
    expected_revision = values.pop("expected_revision")
    _validate_existing_revision(expected_revision)
    supplied = {key: value for key, value in values.items() if value is not _UNSET}
    if not supplied:
        raise ValueError("at least one reader issue field must be supplied")
    if "status" in supplied and supplied["status"] not in READER_ISSUE_STATUSES:
        raise ValueError("invalid reader issue status")
    if "description" in supplied:
        supplied["description"] = _validate_optional_text(
            "description", supplied["description"], maximum=4000
        )
    _validate_anchor_fingerprint_pair(supplied)
    return issue_id, supplied


def _validate_annotation_create(**values: Any) -> dict[str, Any]:
    annotation_id = _validate_uuid("annotation_id", values["annotation_id"])
    _validate_positive_int("paper_id", values["paper_id"])
    if (
        isinstance(values["expected_revision"], bool)
        or not isinstance(values["expected_revision"], int)
        or values["expected_revision"] != 0
    ):
        raise ValueError("new annotations require expected_revision=0")
    kind = values["kind"]
    if kind not in ANNOTATION_KINDS:
        raise ValueError("invalid annotation kind")
    fingerprint = _validate_fingerprint(values["article_fingerprint"])
    anchor = _validate_stable_anchor(
        values["anchor"], article_fingerprint=fingerprint, require_selection=True
    )
    question_status = values["question_status"]
    if kind == "question":
        question_status = "open" if question_status is None else question_status
        if question_status not in QUESTION_STATUSES:
            raise ValueError("invalid question status")
    elif question_status is not None:
        raise ValueError("only question annotations have a question_status")
    return {
        "id": annotation_id,
        "paper_id": values["paper_id"],
        "kind": kind,
        "anchor": anchor,
        "body": _validate_optional_text("body", values["body"], maximum=20000),
        "category": _validate_optional_text(
            "category", values["category"], maximum=80, single_line=True
        ),
        "question_status": question_status,
        "article_fingerprint": fingerprint,
        "ai_share_allowed": _validate_boolean(
            "ai_share_allowed", values["ai_share_allowed"]
        ),
    }


def _validate_annotation_update(**values: Any) -> tuple[str, dict[str, Any]]:
    annotation_id = _validate_uuid("annotation_id", values.pop("annotation_id"))
    expected_revision = values.pop("expected_revision")
    _validate_existing_revision(expected_revision)
    supplied = {key: value for key, value in values.items() if value is not _UNSET}
    if not supplied:
        raise ValueError("at least one annotation field must be supplied")
    if "body" in supplied:
        supplied["body"] = _validate_optional_text(
            "body", supplied["body"], maximum=20000
        )
    if "category" in supplied:
        supplied["category"] = _validate_optional_text(
            "category", supplied["category"], maximum=80, single_line=True
        )
    if (
        "question_status" in supplied
        and supplied["question_status"] not in QUESTION_STATUSES
    ):
        raise ValueError("invalid question status")
    if "ai_share_allowed" in supplied:
        supplied["ai_share_allowed"] = _validate_boolean(
            "ai_share_allowed", supplied["ai_share_allowed"]
        )
    _validate_anchor_fingerprint_pair(supplied, require_selection=True)
    return annotation_id, supplied


def _validate_anchor_fingerprint_pair(
    values: dict[str, Any], *, require_selection: bool = False
) -> None:
    has_anchor = "anchor" in values
    has_fingerprint = "article_fingerprint" in values
    if has_anchor != has_fingerprint:
        raise ValueError("anchor and article_fingerprint must be updated together")
    if has_fingerprint:
        fingerprint = _validate_fingerprint(values["article_fingerprint"])
        values["article_fingerprint"] = fingerprint
        values["anchor"] = _validate_stable_anchor(
            values["anchor"],
            article_fingerprint=fingerprint,
            require_selection=require_selection,
        )


def _validate_stable_anchor(
    value: Any,
    *,
    article_fingerprint: str,
    require_selection: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("anchor must be an object")
    required = {"selector_version", "scope", "article_fingerprint"}
    selection = {"start_utf16", "end_utf16", "exact_quote", "prefix", "suffix"}
    allowed = required | selection | {"section_id", "node_id", "page"}
    if set(value) - allowed:
        raise ValueError("anchor contains unsupported fields")
    if not required <= set(value):
        raise ValueError("anchor is missing stable selector fields")
    if value["selector_version"] != SELECTOR_VERSION or isinstance(
        value["selector_version"], bool
    ):
        raise ValueError("unsupported anchor selector_version")
    if value["article_fingerprint"] != article_fingerprint:
        raise ValueError("anchor article_fingerprint does not match")
    scope = value["scope"]
    if scope not in {"article", "abstract", "body"}:
        raise ValueError("invalid anchor scope")
    section_id = value.get("section_id")
    node_id = value.get("node_id")
    if section_id is not None and (
        not isinstance(section_id, str)
        or not re.fullmatch(r"s-[0-9a-f]{24}", section_id)
    ):
        raise ValueError("invalid anchor section_id")
    if node_id is not None and (
        not isinstance(node_id, str)
        or not re.fullmatch(
            r"n-[0-9a-f]{24}(?:-(?:[2-9]|[1-9][0-9]+))?", node_id
        )
    ):
        raise ValueError("invalid anchor node_id")
    if scope in {"abstract", "body"} and section_id is None:
        raise ValueError("content anchors require a section_id")
    if node_id is not None and section_id is None:
        raise ValueError("node anchors require a section_id")
    if "page" in value:
        _validate_positive_int("anchor page", value["page"])

    selection_present = selection & set(value)
    if selection_present and selection_present != selection:
        raise ValueError("selection anchor fields must be supplied together")
    if require_selection and selection_present != selection:
        raise ValueError("annotations require an exact selection anchor")
    if selection_present:
        if node_id is None:
            raise ValueError("selection anchors require a node_id")
        start = value["start_utf16"]
        end = value["end_utf16"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end <= start
        ):
            raise ValueError("invalid UTF-16 selection offsets")
        quote = _validate_required_text(
            "exact_quote", value["exact_quote"], maximum=20000, strip=False
        )
        if not quote.strip():
            raise ValueError("exact_quote cannot be blank")
        prefix = _validate_required_text(
            "prefix", value["prefix"], maximum=500, strip=False, allow_empty=True
        )
        suffix = _validate_required_text(
            "suffix", value["suffix"], maximum=500, strip=False, allow_empty=True
        )
        if _utf16_length(quote) != end - start:
            raise ValueError("UTF-16 offsets do not match exact_quote")
        value = dict(value)
        value.update(exact_quote=quote, prefix=prefix, suffix=suffix)

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("anchor must be valid UTF-8 JSON") from exc
    if len(encoded) > 32768:
        raise ValueError("anchor is too large")
    return json.loads(encoded.decode("utf-8"))


def _validate_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("article_fingerprint must be a lowercase SHA-256")
    return value


def _validate_uuid(name: str, value: Any) -> str:
    if not isinstance(value, (str, UUID)):
        raise ValueError(f"{name} must be a UUID")
    try:
        parsed = UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    if parsed.int == 0:
        raise ValueError(f"{name} must not be the nil UUID")
    return str(parsed)


def _validate_existing_revision(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision must be a positive integer")


def _validate_boolean(name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _validate_optional_text(
    name: str,
    value: Any,
    *,
    maximum: int,
    single_line: bool = False,
) -> str | None:
    if value is None:
        return None
    return _validate_required_text(
        name, value, maximum=maximum, single_line=single_line
    )


def _validate_required_text(
    name: str,
    value: Any,
    *,
    maximum: int,
    single_line: bool = False,
    strip: bool = True,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = value.strip() if strip else value
    if not normalized and not allow_empty:
        raise ValueError(f"{name} cannot be blank")
    if len(normalized) > maximum:
        raise ValueError(f"{name} is too long")
    if "\x00" in normalized or (single_line and ("\n" in normalized or "\r" in normalized)):
        raise ValueError(f"{name} contains unsupported characters")
    try:
        normalized.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    return normalized


def _utf16_length(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeError as exc:
        raise ValueError("exact_quote must be valid Unicode") from exc


def _validate_article_warnings(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("warnings must be a list")
    if len(value) > 1000:
        raise ValueError("too many article warnings")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        warning = _validate_required_text("warning", item, maximum=1000)
        warning = unicodedata.normalize("NFKC", warning)
        warning = _validate_required_text("warning", warning, maximum=1000)
        if warning not in seen:
            normalized.append(warning)
            seen.add(warning)
    return normalized


def _system_warning_values(
    paper_id: int, warning: str, article_fingerprint: str
) -> dict[str, Any]:
    digest = hashlib.sha256(warning.encode("utf-8")).hexdigest()
    external_key = f"article-warning:v1:{digest}"
    lowered = warning.casefold()
    if "figure" in lowered or "picture" in lowered or "crop" in lowered:
        kind = "figure_crop"
    elif "table" in lowered:
        kind = "table"
    elif "section" in lowered:
        kind = "section_structure"
    elif any(token in lowered for token in ("sentence", "line_break", "hyphen")):
        kind = "sentence_break"
    elif any(
        token in lowered
        for token in ("metadata", "retracted", "title", "author", "doi")
    ):
        kind = "metadata"
    else:
        kind = "extraction_text"
    anchor: dict[str, Any] = {
        "selector_version": SELECTOR_VERSION,
        "scope": "article",
        "article_fingerprint": article_fingerprint,
    }
    page_match = re.search(r"page[-_: ](\d+)", lowered)
    if page_match and int(page_match.group(1)) > 0:
        anchor["page"] = int(page_match.group(1))
    return {
        "id": str(uuid5(_SYSTEM_ISSUE_NAMESPACE, f"{paper_id}:{external_key}")),
        "paper_id": paper_id,
        "kind": kind,
        "description": warning,
        "anchor": anchor,
        "article_fingerprint": article_fingerprint,
        "source": "system",
        "external_key": external_key,
    }


def _reader_issue_create_matches(
    current: dict[str, Any], values: dict[str, Any]
) -> bool:
    if values["source"] == "system":
        # A warning's external key is deliberately independent of an article
        # rebuild. Preserve the first observed anchor and any later human
        # resolution rather than guessing that the warning moved safely.
        return (
            int(current["paper_id"]) == values["paper_id"]
            and current["source"] == "system"
            and current["external_key"] == values["external_key"]
            and current["kind"] == values["kind"]
            and current["description"] == values["description"]
        )
    return (
        int(current["paper_id"]) == values["paper_id"]
        and current["kind"] == values["kind"]
        and current["description"] == values["description"]
        and current["anchor"] == values["anchor"]
        and str(current["article_fingerprint"]) == values["article_fingerprint"]
        and current["source"] == values["source"]
        and current["external_key"] == values["external_key"]
    )


def _annotation_create_matches(
    current: dict[str, Any], values: dict[str, Any]
) -> bool:
    return (
        int(current["paper_id"]) == values["paper_id"]
        and current["kind"] == values["kind"]
        and current["anchor"] == values["anchor"]
        and current["body"] == values["body"]
        and current["category"] == values["category"]
        and current["question_status"] == values["question_status"]
        and str(current["article_fingerprint"]) == values["article_fingerprint"]
        and current["ai_share_allowed"] is values["ai_share_allowed"]
        and int(current["revision"]) == 1
        and current["trashed_at"] is None
    )
