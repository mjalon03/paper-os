"""Resource-bounded worker for staged reader-v2 conversion."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from .database import (
    claim_paper_for_reader_v2,
    complete_reader_v2_paper,
    ensure_schema,
    fail_reader_v2_paper,
    import_article_warnings,
    recover_reader_v2_papers,
    retry_failed_reader_v2_papers,
)
from .pmc_broker_client import PMCBrokerClientFailure, fetch_pmc_article
from .reader_v2_converter_client import (
    ReaderV2ConverterClientFailure,
    convert_pdf_to_directory,
)
from .reader_v2 import (
    PreparedPaperV2Version,
    ReaderV2Failure,
    cleanup_reader_v2_scratch,
    convert_paper_v2,
    prepare_paper_v2_version,
    read_article,
)
from .ops_runtime import worker_heartbeat_from_env
from .reprocess_runner import run_cycle as run_operations_cycle
from .selectors import build_selector_index


def parse_allowed_ids(value: str | None) -> list[int] | None:
    if value is None or not value.strip():
        return None
    if value.strip().casefold() == "all":
        return None
    try:
        values = sorted({int(item.strip()) for item in value.split(",") if item.strip()})
    except ValueError as exc:
        raise RuntimeError("PAPER_READER_V2_ALLOWED_IDS must contain integers") from exc
    if any(value <= 0 for value in values):
        raise RuntimeError("PAPER_READER_V2_ALLOWED_IDS must contain positive integers")
    return values


def parse_force_reprocess(value: str | None) -> bool:
    if value is None or value == "":
        return False
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise RuntimeError("PAPER_READER_V2_FORCE_REPROCESS must be true or false")


def parse_operations_enabled(value: str | None) -> bool:
    if value is None or value == "":
        return False
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise RuntimeError("PAPER_OPS_EXECUTION_ENABLED must be true or false")


def _bounded_operations_integer(
    name: str,
    value: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"{name} is invalid")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{name} is invalid")
    return parsed


def _heartbeat_processing(heartbeat: Any, paper_id: Any) -> bool:
    if (
        isinstance(paper_id, bool)
        or not isinstance(paper_id, int)
        or paper_id < 1
    ):
        return False
    try:
        heartbeat.processing(paper_id)
        return True
    except Exception:
        return False


def _heartbeat_idle(
    heartbeat: Any,
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


def convert_isolated(
    paper: dict,
    data_dir: Path,
    *,
    force_reprocess: bool = False,
) -> tuple[str, str]:
    """Convert through the credential-free Reader v2 converter service."""
    if type(force_reprocess) is not bool:
        raise RuntimeError("PAPER_READER_V2_FORCE_REPROCESS must be true or false")
    return convert_paper_v2(
        paper,
        data_dir,
        force_reprocess=force_reprocess,
        pmc_article_builder=_isolated_pmc_builder,
        pdf_article_converter=_isolated_pdf_converter,
    )


def _isolated_pmc_builder(
    paper: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any] | None:
    try:
        return fetch_pmc_article(paper, output_dir)
    except PMCBrokerClientFailure as exc:
        raise ReaderV2Failure(exc.code) from exc


def _isolated_pdf_converter(
    paper: dict[str, Any],
    data_dir: Path,
    output_dir: Path,
) -> str:
    try:
        return convert_pdf_to_directory(paper, data_dir, output_dir)
    except ReaderV2ConverterClientFailure as exc:
        raise ReaderV2Failure(exc.code) from exc


def prepare_reprocess_isolated(
    paper: dict,
    data_dir: Path,
) -> PreparedPaperV2Version:
    """Build reprocess staging through the same isolated converter boundary."""

    return prepare_paper_v2_version(
        paper,
        data_dir,
        force_reprocess=True,
        pmc_article_builder=_isolated_pmc_builder,
        pdf_article_converter=_isolated_pdf_converter,
    )


def run_worker() -> None:
    cleanup_reader_v2_scratch(remove_all=True)
    ensure_schema()
    recover_reader_v2_papers()
    allowed_ids = parse_allowed_ids(os.environ.get("PAPER_READER_V2_ALLOWED_IDS"))
    force_reprocess = parse_force_reprocess(
        os.environ.get("PAPER_READER_V2_FORCE_REPROCESS")
    )
    operations_enabled = parse_operations_enabled(
        os.environ.get("PAPER_OPS_EXECUTION_ENABLED", "true")
    )
    operations_worker_id = os.environ.get(
        "PAPER_OPS_WORKER_ID", "reader-v2-ops-worker-1"
    )
    operations_lease_seconds = _bounded_operations_integer(
        "PAPER_OPS_LEASE_SECONDS",
        os.environ.get("PAPER_OPS_LEASE_SECONDS", "1800"),
        minimum=60,
        maximum=3_600,
    )
    operations_retry_delay_seconds = _bounded_operations_integer(
        "PAPER_OPS_RETRY_DELAY_SECONDS",
        os.environ.get("PAPER_OPS_RETRY_DELAY_SECONDS", "60"),
        minimum=1,
        maximum=86_400,
    )
    poll_seconds = max(10, int(os.environ.get("PAPER_READER_V2_POLL_SECONDS", "30")))
    data_dir = Path(os.environ.get("PAPER_DATA_DIR", "/data"))
    with worker_heartbeat_from_env("reader-v2-worker-1") as heartbeat:
        while True:
            operations_claimed = False
            if operations_enabled:
                operation_cycle = run_operations_cycle(
                    worker_id=operations_worker_id,
                    data_dir=data_dir,
                    max_jobs=1,
                    lease_seconds=operations_lease_seconds,
                    retry_delay_seconds=operations_retry_delay_seconds,
                    prepare_version=prepare_reprocess_isolated,
                )
                operations_claimed = any(
                    result.job_id is not None for result in operation_cycle.results
                )
            retry_failed_reader_v2_papers()
            paper = claim_paper_for_reader_v2(allowed_ids)
            if paper:
                active = _heartbeat_processing(heartbeat, paper.get("id"))
                successful = False
                try:
                    manifest, source = convert_isolated(
                        paper,
                        data_dir,
                        force_reprocess=force_reprocess,
                    )
                    article = read_article(data_dir, int(paper["id"]))
                    import_article_warnings(
                        int(paper["id"]),
                        warnings=article["warnings"],
                        article_fingerprint=build_selector_index(
                            article
                        ).article_fingerprint,
                    )
                    complete_reader_v2_paper(int(paper["id"]), manifest, source)
                    successful = True
                except ReaderV2Failure as exc:
                    fail_reader_v2_paper(int(paper["id"]), exc.code)
                except Exception:
                    fail_reader_v2_paper(int(paper["id"]), "reader_v2_failed")
                finally:
                    _heartbeat_idle(
                        heartbeat,
                        active,
                        successful=successful,
                    )
                continue
            if operations_claimed:
                continue
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_worker()
