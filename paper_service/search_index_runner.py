"""Periodic, content-safe search-index maintenance for reader-v2 papers.

The storage layer derives a deterministic corpus digest and reuses an existing
immutable generation when nothing changed.  This runner only schedules that
operation for current reader-v2-ready papers.  Results and errors contain IDs,
counts, and sanitized codes only; paper text and annotation bodies are never
logged or returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

from .database import ensure_schema, list_reader_v2_ready_ids
from .ops_runtime import worker_heartbeat_from_env
from .search_store import SearchStoreError, rebuild_paper_search_index


DEFAULT_POLL_SECONDS = 300
MAX_READY_PAPERS = 100_000

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_ERROR_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{0,119}")
_PAPER_STATUSES = frozenset({"rebuilt", "reused", "failed"})
_CYCLE_STATUSES = frozenset({"idle", "succeeded", "partial", "failed", "error"})


class SearchIndexRunnerError(RuntimeError):
    """A sanitized runner or result-boundary failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PaperIndexResult:
    """Safe outcome for one paper; it intentionally carries no content."""

    paper_id: int
    status: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.paper_id, bool) or not isinstance(self.paper_id, int):
            raise RuntimeError("search_index_result_invalid")
        if self.paper_id < 1 or self.status not in _PAPER_STATUSES:
            raise RuntimeError("search_index_result_invalid")
        if self.status == "failed":
            if not isinstance(self.error_code, str) or not _SAFE_ERROR_RE.fullmatch(
                self.error_code
            ):
                raise RuntimeError("search_index_result_invalid")
        elif self.error_code is not None:
            raise RuntimeError("search_index_result_invalid")


@dataclass(frozen=True)
class SearchIndexCycleResult:
    """Aggregate maintenance result safe for metrics or a health endpoint."""

    status: str
    seen: int
    rebuilt: int
    reused: int
    failed: int
    papers: tuple[PaperIndexResult, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        counts = (self.seen, self.rebuilt, self.reused, self.failed)
        if self.status not in _CYCLE_STATUSES or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in counts
        ):
            raise RuntimeError("search_index_cycle_result_invalid")
        if self.seen != self.rebuilt + self.reused + self.failed:
            raise RuntimeError("search_index_cycle_result_invalid")
        if len(self.papers) != self.seen:
            raise RuntimeError("search_index_cycle_result_invalid")
        if self.status == "error":
            if self.seen != 0 or not isinstance(self.error_code, str):
                raise RuntimeError("search_index_cycle_result_invalid")
            if not _SAFE_ERROR_RE.fullmatch(self.error_code):
                raise RuntimeError("search_index_cycle_result_invalid")
        elif self.error_code is not None:
            raise RuntimeError("search_index_cycle_result_invalid")


def _bounded_integer(
    name: str,
    raw: Any,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(raw, bool):
        raise RuntimeError(f"{name}_invalid")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"[0-9]+", raw):
        value = int(raw)
    else:
        raise RuntimeError(f"{name}_invalid")
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name}_invalid")
    return value


def _data_dir(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    raw = os.fspath(value)
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 4_096
        or _CONTROL_RE.search(raw)
    ):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    try:
        details = path.lstat()
    except (OSError, ValueError) as exc:
        raise RuntimeError("PAPER_DATA_DIR_invalid") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    if not os.access(path, os.R_OK | os.X_OK):
        raise RuntimeError("PAPER_DATA_DIR_invalid")
    return path


def _ready_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > MAX_READY_PAPERS:
        raise SearchIndexRunnerError("search_ready_papers_invalid")
    ids: list[int] = []
    for paper_id in value:
        if isinstance(paper_id, bool) or not isinstance(paper_id, int):
            raise SearchIndexRunnerError("search_ready_papers_invalid")
        if paper_id < 1:
            raise SearchIndexRunnerError("search_ready_papers_invalid")
        ids.append(paper_id)
    if ids != sorted(set(ids)):
        raise SearchIndexRunnerError("search_ready_papers_invalid")
    return tuple(ids)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, SearchStoreError):
        code = exc.code
        if isinstance(code, str) and _SAFE_ERROR_RE.fullmatch(code):
            return code
    if isinstance(exc, SearchIndexRunnerError):
        code = exc.code
        if _SAFE_ERROR_RE.fullmatch(code):
            return code
    return "search_index_rebuild_failed"


def _validate_rebuild_result(value: Any, *, paper_id: int) -> bool:
    if not isinstance(value, Mapping) or value.get("paper_id") != paper_id:
        raise SearchIndexRunnerError("search_index_result_invalid")
    reused = value.get("reused_generation")
    if type(reused) is not bool:
        raise SearchIndexRunnerError("search_index_result_invalid")
    return reused


def _heartbeat_processing(heartbeat: Any | None, paper_id: int) -> bool:
    if heartbeat is None:
        return False
    try:
        heartbeat.processing(paper_id)
        return True
    except Exception:
        return False


def _heartbeat_idle(
    heartbeat: Any | None,
    active: bool,
    *,
    successful: bool,
) -> None:
    if not active:
        return
    try:
        heartbeat.idle(successful=successful)
    except Exception:
        return


def execute_cycle(
    *,
    data_dir: str | Path,
    heartbeat: Any | None = None,
) -> SearchIndexCycleResult:
    """Reconcile every current ready paper once, isolating per-paper failures."""

    root = _data_dir(data_dir)
    try:
        paper_ids = _ready_ids(list_reader_v2_ready_ids())
    except Exception as exc:
        code = _safe_error(exc)
        if code == "search_index_rebuild_failed":
            code = "search_ready_papers_failed"
        return SearchIndexCycleResult(
            status="error",
            seen=0,
            rebuilt=0,
            reused=0,
            failed=0,
            error_code=code,
        )
    if not paper_ids:
        return SearchIndexCycleResult(
            status="idle", seen=0, rebuilt=0, reused=0, failed=0
        )

    outcomes: list[PaperIndexResult] = []
    rebuilt = reused = failed = 0
    for paper_id in paper_ids:
        active = _heartbeat_processing(heartbeat, paper_id)
        successful = False
        try:
            result = rebuild_paper_search_index(paper_id, root)
            if _validate_rebuild_result(result, paper_id=paper_id):
                reused += 1
                outcomes.append(PaperIndexResult(paper_id, "reused"))
            else:
                rebuilt += 1
                outcomes.append(PaperIndexResult(paper_id, "rebuilt"))
            successful = True
        except Exception as exc:
            failed += 1
            outcomes.append(
                PaperIndexResult(
                    paper_id,
                    "failed",
                    error_code=_safe_error(exc),
                )
            )
        finally:
            _heartbeat_idle(
                heartbeat,
                active,
                successful=successful,
            )

    status = (
        "succeeded"
        if failed == 0
        else "failed"
        if failed == len(paper_ids)
        else "partial"
    )
    return SearchIndexCycleResult(
        status=status,
        seen=len(paper_ids),
        rebuilt=rebuilt,
        reused=reused,
        failed=failed,
        papers=tuple(outcomes),
    )


def run_worker() -> None:
    """Run fixed-delay index reconciliation in one sequential process."""

    poll_seconds = _bounded_integer(
        "PAPER_SEARCH_INDEX_POLL_SECONDS",
        os.environ.get(
            "PAPER_SEARCH_INDEX_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)
        ),
        minimum=5,
        maximum=86_400,
    )
    data_dir = _data_dir(os.environ.get("PAPER_DATA_DIR", "/data"))
    ensure_schema()
    with worker_heartbeat_from_env("search-index-worker-1") as heartbeat:
        while True:
            try:
                execute_cycle(data_dir=data_dir, heartbeat=heartbeat)
            except Exception:
                # The loop deliberately emits no exception text: downstream errors
                # can contain source paths or provider/database details.
                pass
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "PaperIndexResult",
    "SearchIndexCycleResult",
    "SearchIndexRunnerError",
    "execute_cycle",
    "run_worker",
]
