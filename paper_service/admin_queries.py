"""Bounded, display-only queries for the private paper administrator UI."""

from __future__ import annotations

import re
from typing import Any

from .database import connection


MAX_ADMIN_PAPER_CHOICES = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class AdminQueryError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> AdminQueryError:
    return AdminQueryError(code)


def _limit(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_ADMIN_PAPER_CHOICES
    ):
        raise _fail("admin_paper_limit_invalid")
    return value


def _display_text(
    value: Any, *, field: str, maximum: int, fallback: str | None = None
) -> str:
    if value is None and fallback is not None:
        return fallback
    if not isinstance(value, str):
        raise _fail(f"admin_paper_{field}_invalid")
    clean = value.strip()
    if not clean and fallback is not None:
        return fallback
    if not clean or len(clean) > maximum or _CONTROL_RE.search(clean):
        raise _fail(f"admin_paper_{field}_invalid")
    return clean


def list_admin_paper_choices(*, limit: int = 100) -> tuple[dict[str, Any], ...]:
    """Return only metadata needed to choose papers for a human action."""

    maximum = _limit(limit)
    with connection() as database:
        rows = database.execute(
            """SELECT id, title, category, publication_year, reader_v2_status
               FROM papers
               WHERE processing_status = 'organized'
               ORDER BY imported_at DESC, id DESC
               LIMIT %s""",
            (maximum,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        paper_id = row.get("id")
        if isinstance(paper_id, bool) or not isinstance(paper_id, int) or paper_id < 1:
            raise _fail("admin_paper_id_invalid")
        year = row.get("publication_year")
        if year is not None and (
            isinstance(year, bool)
            or not isinstance(year, int)
            or not 1_000 <= year <= 9_999
        ):
            raise _fail("admin_paper_publication_year_invalid")
        reader_status = row.get("reader_v2_status")
        if reader_status not in {"pending", "processing", "ready", "failed"}:
            raise _fail("admin_paper_reader_status_invalid")
        output.append(
            {
                "paper_id": paper_id,
                "title": _display_text(
                    row.get("title"),
                    field="title",
                    maximum=1_000,
                    fallback="Untitled Paper",
                ),
                "category": _display_text(
                    row.get("category"),
                    field="category",
                    maximum=120,
                    fallback="Uncategorized",
                ),
                "publication_year": year,
                "reader_status": reader_status,
            }
        )
    return tuple(output)


def get_admin_paper_source_binding(paper_id: int) -> dict[str, Any] | None:
    """Read the opaque DB source identity needed to bind an operation request."""

    if (
        isinstance(paper_id, bool)
        or not isinstance(paper_id, int)
        or paper_id < 1
    ):
        raise _fail("admin_paper_id_invalid")
    with connection() as database:
        raw = database.execute(
            """SELECT id, sha256 FROM papers
               WHERE id = %s AND processing_status = 'organized'""",
            (paper_id,),
        ).fetchone()
    if raw is None:
        return None
    row = dict(raw)
    stored_id = row.get("id")
    digest = row.get("sha256")
    if stored_id != paper_id or not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise _fail("admin_paper_source_binding_invalid")
    return {"paper_id": paper_id, "source_sha256": digest}


__all__ = [
    "AdminQueryError",
    "MAX_ADMIN_PAPER_CHOICES",
    "get_admin_paper_source_binding",
    "list_admin_paper_choices",
]
