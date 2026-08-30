"""Coordinator client for the DOI-only, egress-isolated PMC broker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request
import zipfile

from .article import ArticleValidationError, validate_article
from .artifact_versions import MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES
from .pmc_broker_contract import (
    PMC_BROKER_MAX_BUNDLE_BYTES,
    PMC_BROKER_MAX_METADATA_BYTES,
    PMC_BROKER_METADATA_NAME,
    PMCBrokerContractFailure,
    build_request,
    canonical_json_bytes,
    payload_sha256,
    validate_result,
)
from .reader_v2_job import ReaderV2JobFailure, build_output_manifest


class PMCBrokerClientFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any):
        return None


_OPENER = build_opener(_NoRedirectHandler)


def _base_url(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    raw = values.get("PAPER_PMC_BROKER_URL", "http://paper-pmc-broker:8092")
    parsed = urlsplit(raw)
    allowed = {
        item.strip()
        for item in values.get(
            "PAPER_PMC_BROKER_ALLOWED_HOSTS", "paper-pmc-broker"
        ).split(",")
        if item.strip()
    }
    try:
        port = parsed.port
    except ValueError as exc:
        raise PMCBrokerClientFailure("pmc_broker_configuration_invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise PMCBrokerClientFailure("pmc_broker_configuration_invalid")
    return raw.rstrip("/")


def _timeout(env: Mapping[str, str] | None = None) -> int:
    values = os.environ if env is None else env
    raw = values.get("PAPER_PMC_BROKER_TIMEOUT_SECONDS", "180")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        raise PMCBrokerClientFailure("pmc_broker_configuration_invalid")
    value = int(raw)
    if not 20 <= value <= 600:
        raise PMCBrokerClientFailure("pmc_broker_configuration_invalid")
    return value


def _download_bundle(request_value: Mapping[str, Any], destination: Path) -> bool:
    encoded = canonical_json_bytes(request_value)
    request = Request(
        f"{_base_url()}/v1/article",
        data=encoded,
        headers={"Content-Type": "application/json", "Accept": "application/zip"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=_timeout()) as response:
            if response.status == 204:
                return False
            if (
                response.status != 200
                or response.headers.get_content_type() != "application/zip"
            ):
                raise PMCBrokerClientFailure("pmc_broker_response_invalid")
            declared = response.headers.get("Content-Length")
            try:
                expected = int(declared) if declared is not None else -1
            except ValueError as exc:
                raise PMCBrokerClientFailure("pmc_broker_response_invalid") from exc
            if not 1 <= expected <= PMC_BROKER_MAX_BUNDLE_BYTES:
                raise PMCBrokerClientFailure("pmc_broker_response_invalid")
            descriptor = os.open(
                destination,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            total = 0
            try:
                while chunk := response.read(
                    min(1024 * 1024, PMC_BROKER_MAX_BUNDLE_BYTES + 1 - total)
                ):
                    total += len(chunk)
                    if total > PMC_BROKER_MAX_BUNDLE_BYTES:
                        raise PMCBrokerClientFailure("pmc_broker_response_too_large")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise PMCBrokerClientFailure(
                                "pmc_broker_response_write_failed"
                            )
                        view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if total != expected:
                raise PMCBrokerClientFailure("pmc_broker_response_invalid")
            return True
    except HTTPError:
        # Eligibility misses and broker source failures safely fall back to PDF.
        return False
    except (OSError, TimeoutError, URLError):
        return False


def _zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in archive.infolist():
        name = info.filename
        if (
            name in entries
            or info.is_dir()
            or info.flag_bits & 0x1
            or (
                name != PMC_BROKER_METADATA_NAME
                and name != "article.json"
                and re.fullmatch(r"assets/[A-Za-z0-9._-]{1,180}", name) is None
            )
        ):
            raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in {0, stat.S_IFREG}:
            raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
        maximum = (
            PMC_BROKER_MAX_METADATA_BYTES
            if name == PMC_BROKER_METADATA_NAME
            else MAX_FILE_BYTES
        )
        if not 0 <= info.file_size <= maximum:
            raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
        total += info.file_size
        if total > MAX_TOTAL_BYTES + PMC_BROKER_MAX_METADATA_BYTES:
            raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
        entries[name] = info
    if not 2 <= len(entries) <= MAX_FILES + 1:
        raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
    if PMC_BROKER_METADATA_NAME not in entries or "article.json" not in entries:
        raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
    return entries


def _read_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum: int,
) -> bytes:
    with archive.open(info, "r") as handle:
        payload = handle.read(maximum + 1)
        if len(payload) > maximum or handle.read(1):
            raise PMCBrokerClientFailure("pmc_broker_bundle_invalid")
        return payload


def _extract_verified_bundle(
    bundle: Path,
    *,
    output_dir: Path,
    request_value: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            entries = _zip_entries(archive)
            metadata = validate_result(
                json.loads(
                    _read_entry(
                        archive,
                        entries[PMC_BROKER_METADATA_NAME],
                        maximum=PMC_BROKER_MAX_METADATA_BYTES,
                    ).decode("utf-8")
                )
            )
            if (
                metadata["request_id"] != request_value["request_id"]
                or metadata["request_sha256"] != payload_sha256(request_value)
                or set(metadata["files"])
                != set(entries) - {PMC_BROKER_METADATA_NAME}
            ):
                raise PMCBrokerClientFailure("pmc_broker_result_mismatch")
            for relative, expected in metadata["files"].items():
                info = entries[relative]
                if info.file_size != expected["size"]:
                    raise PMCBrokerClientFailure("pmc_broker_result_mismatch")
                destination = output_dir / relative
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                digest = hashlib.sha256()
                total = 0
                try:
                    with archive.open(info, "r") as source:
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > expected["size"]:
                                raise PMCBrokerClientFailure(
                                    "pmc_broker_result_mismatch"
                                )
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    raise PMCBrokerClientFailure(
                                        "pmc_broker_result_write_failed"
                                    )
                                view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if total != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise PMCBrokerClientFailure("pmc_broker_result_mismatch")
            if build_output_manifest(output_dir) != metadata["files"]:
                raise PMCBrokerClientFailure("pmc_broker_result_mismatch")
            article = validate_article(
                json.loads(
                    (output_dir / "article.json").read_text(encoding="utf-8")
                )
            )
            if article.get("provenance", {}).get("source") != "pmc_jats":
                raise PMCBrokerClientFailure("pmc_broker_result_mismatch")
            return article
    except PMCBrokerClientFailure:
        raise
    except (
        ArticleValidationError,
        PMCBrokerContractFailure,
        ReaderV2JobFailure,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
    ) as exc:
        raise PMCBrokerClientFailure("pmc_broker_bundle_invalid") from exc


def fetch_pmc_article(
    paper: Mapping[str, Any],
    output_dir: Path,
) -> dict[str, Any] | None:
    """Return a validated PMC article or None so the caller can use PDF."""

    try:
        request_value = build_request(
            identifier=f"pmc-{secrets.token_hex(16)}",
            paper=paper,
        )
    except PMCBrokerContractFailure:
        return None
    if not isinstance(output_dir, Path) or not output_dir.is_absolute():
        raise PMCBrokerClientFailure("pmc_broker_output_invalid")
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".pmc-bundle-",
        suffix=".zip",
        dir=output_dir.parent,
    )
    os.close(descriptor)
    bundle = Path(raw_path)
    bundle.unlink()
    try:
        if not _download_bundle(request_value, bundle):
            return None
        return _extract_verified_bundle(
            bundle,
            output_dir=output_dir,
            request_value=request_value,
        )
    finally:
        try:
            details = bundle.lstat()
            if stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                bundle.unlink()
        except FileNotFoundError:
            pass


__all__ = ["PMCBrokerClientFailure", "fetch_pmc_article"]
