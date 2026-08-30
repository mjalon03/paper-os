"""Egress-only PMC broker; it receives a DOI, never a caller-supplied URL."""

from __future__ import annotations

from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
from typing import Any, Iterator, Mapping
import zipfile

from .article import ArticleValidationError, validate_article
from .jats import JATSFailure
from .pmc import PMCFailure
from .pmc_broker_contract import (
    PMC_BROKER_MAX_BUNDLE_BYTES,
    PMC_BROKER_MAX_REQUEST_BYTES,
    PMC_BROKER_METADATA_NAME,
    PMCBrokerContractFailure,
    canonical_json_bytes,
    payload_sha256,
    result_metadata,
    validate_request,
)
from .reader_v2 import build_pmc_article, write_article
from .reader_v2_job import ReaderV2JobFailure, build_output_manifest


_CONCURRENCY = threading.BoundedSemaphore(1)
_WORKSPACE_RE = re.compile(r"pmc-[0-9a-f]{32}")


class PMCBrokerServiceFailure(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 422):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _scratch_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    root = Path(values.get("PAPER_PMC_BROKER_SCRATCH_DIR", "/work/pmc"))
    if not root.is_absolute():
        raise PMCBrokerServiceFailure(
            "pmc_broker_configuration_invalid", http_status=503
        )
    try:
        details = root.lstat()
    except OSError as exc:
        raise PMCBrokerServiceFailure(
            "pmc_broker_scratch_unavailable", http_status=503
        ) from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise PMCBrokerServiceFailure(
            "pmc_broker_scratch_unavailable", http_status=503
        )
    return root


@contextmanager
def _workspace() -> Iterator[Path]:
    root = _scratch_root()
    identifier = f"pmc-{secrets.token_hex(16)}"
    path = root / identifier
    path.mkdir(mode=0o700)
    try:
        yield path
    finally:
        try:
            details = path.lstat()
            owned = (
                _WORKSPACE_RE.fullmatch(path.name) is not None
                and path.parent == root
                and stat.S_ISDIR(details.st_mode)
                and not stat.S_ISLNK(details.st_mode)
            )
        except OSError:
            owned = False
        if owned:
            shutil.rmtree(path, ignore_errors=True)


def _bundle(
    destination: Path,
    *,
    output: Path,
    metadata: Mapping[str, Any],
) -> None:
    files = metadata["files"]
    with zipfile.ZipFile(
        destination,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        info = zipfile.ZipInfo(PMC_BROKER_METADATA_NAME, (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100600 << 16
        archive.writestr(info, canonical_json_bytes(metadata))
        for relative in sorted(files):
            source = output / relative
            details = source.lstat()
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise PMCBrokerServiceFailure("pmc_broker_artifact_invalid")
            archive.write(source, relative)
    destination.chmod(0o600)


def build_bundle(value: Any, workspace: Path) -> Path | None:
    try:
        request = validate_request(value)
    except PMCBrokerContractFailure as exc:
        raise PMCBrokerServiceFailure(exc.code) from exc
    output = workspace / "output"
    output.mkdir(mode=0o700)
    try:
        article = build_pmc_article(request["paper"], output)
    except (PMCFailure, JATSFailure, ArticleValidationError) as exc:
        code = getattr(exc, "code", "pmc_broker_source_failed")
        status = 422 if code in {"pmc_doi_invalid"} else 503
        raise PMCBrokerServiceFailure(code, http_status=status) from exc
    if article is None:
        return None
    try:
        write_article(validate_article(article), output)
        files = build_output_manifest(output)
    except (ArticleValidationError, ReaderV2JobFailure, OSError) as exc:
        raise PMCBrokerServiceFailure("pmc_broker_artifact_invalid") from exc
    metadata = result_metadata(
        identifier=request["request_id"],
        request_sha256=payload_sha256(request),
        files=files,
    )
    destination = workspace / "pmc-result.zip"
    _bundle(destination, output=output, metadata=metadata)
    return destination


class PMCBrokerRequestHandler(BaseHTTPRequestHandler):
    server_version = "paper-pmc-broker"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/healthz":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "missing"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/article":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "pmc_broker_operation_invalid"},
            )
            return
        acquired = False
        try:
            length_text = self.headers.get("Content-Length")
            length = int(length_text) if length_text is not None else -1
            if not 1 <= length <= PMC_BROKER_MAX_REQUEST_BYTES:
                raise PMCBrokerServiceFailure(
                    "pmc_broker_request_too_large", http_status=413
                )
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise PMCBrokerServiceFailure("pmc_broker_request_invalid")
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PMCBrokerServiceFailure("pmc_broker_request_invalid") from exc
            acquired = _CONCURRENCY.acquire(blocking=False)
            if not acquired:
                raise PMCBrokerServiceFailure("pmc_broker_busy", http_status=503)
            with _workspace() as workspace:
                bundle = build_bundle(value, workspace)
                if bundle is None:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                size = bundle.stat().st_size
                if not 1 <= size <= PMC_BROKER_MAX_BUNDLE_BYTES:
                    raise PMCBrokerServiceFailure(
                        "pmc_broker_bundle_too_large", http_status=503
                    )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with bundle.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        self.wfile.write(chunk)
        except PMCBrokerServiceFailure as exc:
            self._send_json(exc.http_status, {"error": exc.code})
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "pmc_broker_service_failed"},
            )
        finally:
            if acquired:
                _CONCURRENCY.release()


def run_server() -> None:
    raw = os.environ.get("PAPER_PMC_BROKER_PORT", "8092")
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 65_535:
        raise RuntimeError("PAPER_PMC_BROKER_PORT invalid")
    server = ThreadingHTTPServer(("0.0.0.0", int(raw)), PMCBrokerRequestHandler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    run_server()
