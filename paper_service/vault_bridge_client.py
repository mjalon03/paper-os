"""Mac-side at-least-once bridge from reviewed outbox to Obsidian.

Cloudflare service-token files remain on the Mac.  The client leases exactly
one reviewed payload, writes it through ``VaultSyncWriter``, and acknowledges
only after the writer's readback and ledger checks succeed.  A lost ACK is
safe: the next lease reaches the writer's idempotent ``already_imported`` path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID

from .knowledge import (
    KnowledgeValidationError,
    payload_sha256,
    validate_knowledge_payload,
)
from .vault_sync import ImportResult, VaultSyncError, VaultSyncWriter


DEFAULT_ORIGIN = "https://papers.minjae-jo.com"
MAX_RESPONSE_BYTES = 2_000_000
_SERVICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\.access")
_CONSUMER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_ERROR_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_:-]{0,119}")


class VaultBridgeError(RuntimeError):
    """A sanitized client, transport, or outbox contract failure."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, repr=False)
class ServiceCredentials:
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        if not _SERVICE_ID_RE.fullmatch(self.client_id):
            raise _fail("client_id_invalid")
        if not re.fullmatch(r"[!-~]{20,1024}", self.client_secret):
            raise _fail("client_secret_invalid")


@dataclass(frozen=True)
class OutboxLease:
    outbox_id: str
    paper_id: int
    revision_id: str
    payload_sha256: str
    lease_token: str
    lease_expires_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class VaultBridgeResult:
    status: str
    outbox_id: str | None = None
    revision_id: str | None = None
    import_result: str | None = None
    relative_path: str | None = None


def _fail(code: str, *, retryable: bool = False) -> VaultBridgeError:
    return VaultBridgeError(code, retryable=retryable)


def _private_file(path_value: str | Path, *, field: str, limit: int) -> str:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise _fail(f"{field}_file_invalid")
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) & 0o077
            or not 1 <= details.st_size <= limit
        ):
            raise _fail(f"{field}_file_invalid")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != details.st_dev
                or opened.st_ino != details.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) & 0o077
                or not 1 <= opened.st_size <= limit
            ):
                raise _fail(f"{field}_file_changed")
            chunks: list[bytes] = []
            remaining = min(opened.st_size, limit + 1)
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != opened.st_size:
                raise _fail(f"{field}_file_changed")
        finally:
            os.close(descriptor)
        value = raw.decode("utf-8").strip()
    except VaultBridgeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _fail(f"{field}_file_invalid") from exc
    return value


def load_service_credentials(
    client_id_file: str | Path, client_secret_file: str | Path
) -> ServiceCredentials:
    client_id = _private_file(client_id_file, field="client_id", limit=512)
    client_secret = _private_file(
        client_secret_file, field="client_secret", limit=2_048
    )
    if not _SERVICE_ID_RE.fullmatch(client_id):
        raise _fail("client_id_invalid")
    if not re.fullmatch(r"[!-~]{20,1024}", client_secret):
        raise _fail("client_secret_invalid")
    return ServiceCredentials(client_id, client_secret)


def _origin(value: str) -> str:
    if not isinstance(value, str):
        raise _fail("bridge_origin_invalid")
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or parsed.hostname != "papers.minjae-jo.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _fail("bridge_origin_invalid")
    return DEFAULT_ORIGIN


def _consumer_id(value: str) -> str:
    if not isinstance(value, str) or not _CONSUMER_ID_RE.fullmatch(value):
        raise _fail("consumer_id_invalid")
    return value


def _uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise _fail(f"{field}_invalid")
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise _fail(f"{field}_invalid")
    return value


def _canonical_body(value: dict[str, Any]) -> bytes:
    try:
        result = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("bridge_request_invalid") from exc
    if not result or len(result) > 100_000:
        raise _fail("bridge_request_size_invalid")
    return result


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("bridge_response_duplicate_key")
        result[key] = value
    return result


def _load_response(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise _fail("bridge_response_size_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda item: (_ for _ in ()).throw(
                _fail(f"bridge_response_non_finite_{item}")
            ),
        )
    except VaultBridgeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail("bridge_response_invalid_json") from exc
    if not isinstance(value, dict):
        raise _fail("bridge_response_shape_invalid")
    return value


def validate_outbox_lease(value: Any) -> OutboxLease | None:
    if not isinstance(value, dict) or set(value) != {"item"}:
        raise _fail("outbox_lease_envelope_invalid")
    item = value["item"]
    if item is None:
        return None
    expected = {
        "outbox_id",
        "paper_id",
        "revision_id",
        "payload_sha256",
        "lease_token",
        "lease_expires_at",
        "payload",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise _fail("outbox_lease_item_invalid")
    paper_id = item["paper_id"]
    if isinstance(paper_id, bool) or not isinstance(paper_id, int) or paper_id <= 0:
        raise _fail("outbox_paper_id_invalid")
    expires_at = item["lease_expires_at"]
    if not isinstance(expires_at, str) or len(expires_at) > 64:
        raise _fail("outbox_lease_expiry_invalid")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail("outbox_lease_expiry_invalid") from exc
    if expiry.tzinfo is None:
        raise _fail("outbox_lease_expiry_invalid")
    try:
        canonical = validate_knowledge_payload(item["payload"])
    except KnowledgeValidationError as exc:
        raise _fail("outbox_payload_invalid") from exc
    if canonical["status"] != "reviewed":
        raise _fail("outbox_payload_not_reviewed")
    revision_id = _uuid(item["revision_id"], field="revision_id")
    if canonical["revision_id"] != revision_id:
        raise _fail("outbox_revision_mismatch")
    if canonical["paper_identity"]["paper_id"] != paper_id:
        raise _fail("outbox_paper_mismatch")
    digest = _sha256(item["payload_sha256"], field="payload_sha256")
    if payload_sha256(canonical) != digest:
        raise _fail("outbox_payload_digest_mismatch")
    return OutboxLease(
        outbox_id=_uuid(item["outbox_id"], field="outbox_id"),
        paper_id=paper_id,
        revision_id=revision_id,
        payload_sha256=digest,
        lease_token=_uuid(item["lease_token"], field="lease_token"),
        lease_expires_at=expires_at,
        payload=canonical,
    )


class VaultBridgeClient:
    def __init__(
        self,
        *,
        credentials: ServiceCredentials,
        consumer_id: str,
        origin: str = DEFAULT_ORIGIN,
        timeout_seconds: int = 30,
        open_request: Callable[[urllib.request.Request, int], Any] = urllib.request.urlopen,
    ) -> None:
        if not isinstance(credentials, ServiceCredentials):
            raise _fail("service_credentials_invalid")
        self.credentials = credentials
        self.consumer_id = _consumer_id(consumer_id)
        self.origin = _origin(origin)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 120
        ):
            raise _fail("bridge_timeout_invalid")
        self.timeout_seconds = timeout_seconds
        self.open_request = open_request

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"/api/knowledge/outbox(?:/[A-Za-z0-9-]+){1,3}", path):
            raise _fail("bridge_path_invalid")
        request = urllib.request.Request(
            self.origin + path,
            data=_canonical_body(body),
            headers={
                "CF-Access-Client-Id": self.credentials.client_id,
                "CF-Access-Client-Secret": self.credentials.client_secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "paper-os-vault-bridge/0.6",
            },
            method="POST",
        )
        try:
            with self.open_request(request, self.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        length = int(content_length)
                    except ValueError as exc:
                        raise _fail("bridge_content_length_invalid") from exc
                    if length < 1 or length > MAX_RESPONSE_BYTES:
                        raise _fail("bridge_response_size_invalid")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except VaultBridgeError:
            raise
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code <= 599
            code = (
                "bridge_auth_failed"
                if exc.code in {401, 403}
                else "bridge_rate_limited"
                if exc.code == 429
                else "bridge_server_error"
                if exc.code >= 500
                else "bridge_request_rejected"
            )
            raise _fail(code, retryable=retryable) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _fail("bridge_unavailable", retryable=True) from exc
        return _load_response(raw)

    def lease(self, *, lease_seconds: int = 300) -> OutboxLease | None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= 3_600
        ):
            raise _fail("lease_seconds_invalid")
        envelope = self._post(
            "/api/knowledge/outbox/lease",
            {"consumer_id": self.consumer_id, "lease_seconds": lease_seconds},
        )
        return validate_outbox_lease(envelope)

    def acknowledge(self, lease: OutboxLease) -> None:
        response = self._post(
            f"/api/knowledge/outbox/{lease.outbox_id}/ack",
            {
                "consumer_id": self.consumer_id,
                "lease_token": lease.lease_token,
                "payload_sha256": lease.payload_sha256,
            },
        )
        if response != {"status": "delivered"}:
            raise _fail("outbox_ack_response_invalid")

    def reject(
        self,
        lease: OutboxLease,
        *,
        error_code: str,
        retry_delay_seconds: int = 300,
    ) -> None:
        if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
            error_code = "vault_import_failed"
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or not 30 <= retry_delay_seconds <= 86_400
        ):
            raise _fail("retry_delay_seconds_invalid")
        response = self._post(
            f"/api/knowledge/outbox/{lease.outbox_id}/nack",
            {
                "consumer_id": self.consumer_id,
                "lease_token": lease.lease_token,
                "error_code": error_code,
                "retry_delay_seconds": retry_delay_seconds,
            },
        )
        if response not in ({"status": "pending"}, {"status": "dead"}):
            raise _fail("outbox_nack_response_invalid")


def sync_reviewed_knowledge_once(
    client: VaultBridgeClient,
    writer: VaultSyncWriter,
) -> VaultBridgeResult:
    """Lease, durably import, then ACK one item; never ACK a failed import."""

    lease = client.lease()
    if lease is None:
        return VaultBridgeResult(status="idle")
    try:
        imported: ImportResult = writer.import_payload(lease.payload)
    except VaultSyncError as exc:
        code = str(exc)
        client.reject(
            lease,
            error_code=code if _ERROR_CODE_RE.fullmatch(code) else "vault_import_failed",
        )
        raise _fail("vault_import_failed") from exc
    client.acknowledge(lease)
    return VaultBridgeResult(
        status="delivered",
        outbox_id=lease.outbox_id,
        revision_id=lease.revision_id,
        import_result=imported.result,
        relative_path=imported.relative_path,
    )


def bridge_result_json(result: VaultBridgeResult) -> str:
    return json.dumps(
        asdict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "DEFAULT_ORIGIN",
    "OutboxLease",
    "ServiceCredentials",
    "VaultBridgeClient",
    "VaultBridgeError",
    "VaultBridgeResult",
    "bridge_result_json",
    "load_service_credentials",
    "sync_reviewed_knowledge_once",
    "validate_outbox_lease",
]
