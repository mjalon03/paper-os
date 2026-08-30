"""Resolve eligible PMC Open Access JATS and media without legacy OA APIs."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable


ID_CONVERTER = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
S3_HOST = "pmc-oa-opendata.s3.amazonaws.com"
S3_ROOT = f"https://{S3_HOST}"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MEDIA_BYTES = 40 * 1024 * 1024
PMCID_RE = re.compile(r"PMC[1-9]\d*")
VERSION_RE = re.compile(r"(PMC[1-9]\d*)\.(\d+)")
DOI_RE = re.compile(r"10\.\d{4,9}/[!-~]{1,240}", re.IGNORECASE)
Fetch = Callable[[str, int], bytes]
_PMC_HTTPS_HOSTS = frozenset(
    {"www.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", S3_HOST}
)


class PMCFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_doi(value: str) -> str:
    doi = value.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE).strip().casefold()
    if not DOI_RE.fullmatch(doi) or any(
        character in doi for character in ('"', "'", "<", ">", "\\")
    ):
        raise PMCFailure("pmc_doi_invalid")
    return doi


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, *, timeout: float):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_address, 443),
            self.timeout,
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _public_addresses(hostname: str) -> tuple[str, ...]:
    try:
        values = {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname,
                443,
                type=socket.SOCK_STREAM,
            )
        }
        addresses = tuple(sorted(values))
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise PMCFailure("pmc_address_rejected")
        return addresses
    except PMCFailure:
        raise
    except (OSError, ValueError) as exc:
        raise PMCFailure("pmc_address_rejected") from exc


def _open_pmc_connection(hostname: str, address: str) -> _PinnedHTTPSConnection:
    return _PinnedHTTPSConnection(hostname, address, timeout=20)


def _fetch(url: str, limit: int) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PMCFailure("pmc_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _PMC_HTTPS_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
        or not parsed.path.startswith("/")
    ):
        raise PMCFailure("pmc_url_invalid")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_MEDIA_BYTES
    ):
        raise PMCFailure("pmc_response_limit_invalid")
    target = parsed.path + (("?" + parsed.query) if parsed.query else "")
    addresses = _public_addresses(str(parsed.hostname))
    connection = None
    response = None
    try:
        last_error: BaseException | None = None
        for address in addresses:
            try:
                connection = _open_pmc_connection(str(parsed.hostname), address)
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Accept": "*/*",
                        "User-Agent": "PersonalPaperReader/0.6 (private research tool)",
                    },
                )
                response = connection.getresponse()
                break
            except Exception as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                    connection = None
        if response is None:
            raise PMCFailure("pmc_request_failed") from last_error
        if 300 <= response.status <= 399:
            raise PMCFailure("pmc_redirect_denied")
        if response.status != 200:
            raise PMCFailure("pmc_request_failed")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise PMCFailure("pmc_response_invalid") from exc
            if declared_size < 0 or declared_size > limit:
                raise PMCFailure("pmc_response_too_large")
        payload = response.read(limit + 1)
    except PMCFailure:
        raise
    except Exception as exc:
        raise PMCFailure("pmc_request_failed") from exc
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
    if len(payload) > limit:
        raise PMCFailure("pmc_response_too_large")
    return payload


def resolve_pmcid(doi: str, fetch: Fetch = _fetch) -> str | None:
    normalized = normalize_doi(doi)
    query = urllib.parse.urlencode({"format": "json", "ids": normalized})
    try:
        data = json.loads(fetch(f"{ID_CONVERTER}?{query}", MAX_RESPONSE_BYTES))
    except PMCFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PMCFailure("pmc_id_response_invalid") from exc
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        return None
    pmcid = records[0].get("pmcid") if isinstance(records[0], dict) else None
    return pmcid if isinstance(pmcid, str) and PMCID_RE.fullmatch(pmcid) else None


def _listing_versions(payload: bytes, pmcid: str) -> list[int]:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.IGNORECASE):
        raise PMCFailure("pmc_listing_invalid")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PMCFailure("pmc_listing_invalid") from exc
    versions: set[int] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] not in {"Prefix", "Key"} or not element.text:
            continue
        match = VERSION_RE.search(element.text)
        if match and match.group(1) == pmcid:
            versions.add(int(match.group(2)))
    return sorted(versions, reverse=True)


def _metadata_url(pmcid: str, version: int) -> str:
    prefix = f"{pmcid}.{version}"
    return f"{S3_ROOT}/{prefix}/{prefix}.json"


def _s3_https(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "s3" and parsed.netloc == "pmc-oa-opendata":
        path = parsed.path.lstrip("/")
        return f"{S3_ROOT}/{urllib.parse.quote(path, safe='/._-')}"
    if parsed.scheme == "https" and parsed.hostname == S3_HOST:
        return value
    raise PMCFailure("pmc_asset_url_invalid")


def _eligible(metadata: dict[str, Any], pmcid: str, version: int) -> bool:
    prefix = f"{pmcid}.{version}"
    recorded = str(metadata.get("pmcid") or metadata.get("article_id") or "")
    if recorded and pmcid not in recorded:
        return False
    if metadata.get("is_pmc_openaccess") is not True:
        return False
    license_code = str(metadata.get("license_code") or "").upper().strip()
    if not license_code.startswith("CC"):
        return False
    try:
        xml_url = _s3_https(str(metadata["xml_url"]))
    except (KeyError, PMCFailure):
        return False
    return urllib.parse.urlsplit(xml_url).path.lstrip("/").startswith(f"{prefix}/")


def discover_article(doi: str, fetch: Fetch = _fetch) -> dict[str, Any] | None:
    pmcid = resolve_pmcid(doi, fetch)
    if not pmcid:
        return None
    query = urllib.parse.urlencode(
        {"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"}
    )
    versions = _listing_versions(fetch(f"{S3_ROOT}/?{query}", MAX_RESPONSE_BYTES), pmcid)
    for version in versions:
        try:
            metadata = json.loads(fetch(_metadata_url(pmcid, version), MAX_RESPONSE_BYTES))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PMCFailure("pmc_metadata_invalid") from exc
        if not isinstance(metadata, dict) or not _eligible(metadata, pmcid, version):
            continue
        metadata = dict(metadata)
        metadata["resolved_pmcid"] = pmcid
        metadata["resolved_version"] = version
        metadata["xml_url"] = _s3_https(str(metadata["xml_url"]))
        return metadata
    return None


def fetch_jats(metadata: dict[str, Any], fetch: Fetch = _fetch) -> bytes:
    return fetch(_s3_https(str(metadata.get("xml_url", ""))), MAX_RESPONSE_BYTES)


def media_urls(metadata: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    values = metadata.get("media_urls")
    if not isinstance(values, list):
        return resolved
    prefix = f'{metadata.get("resolved_pmcid")}.{metadata.get("resolved_version")}/'
    for item in values:
        raw = item.get("url") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            continue
        try:
            url = _s3_https(raw)
        except PMCFailure:
            continue
        parsed = urllib.parse.urlsplit(url)
        name = Path(parsed.path).name
        if (
            parsed.path.lstrip("/").startswith(prefix)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,180}", name)
        ):
            resolved.setdefault(name, url)
    return resolved


def download_media(
    url: str,
    output_root: Path,
    asset_name: str,
    fetch: Fetch = _fetch,
    expected_md5: str | None = None,
) -> tuple[Path, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != S3_HOST:
        raise PMCFailure("pmc_asset_url_invalid")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", asset_name):
        raise PMCFailure("pmc_asset_name_invalid")
    root = output_root.resolve()
    destination = (root / asset_name).resolve()
    if destination.parent != root:
        raise PMCFailure("pmc_asset_path_invalid")
    payload = fetch(url, MAX_MEDIA_BYTES)
    digest = hashlib.md5(payload).hexdigest()  # nosec B324 - dataset integrity only
    if expected_md5 and not re.fullmatch(r"[a-fA-F0-9]{32}", expected_md5):
        raise PMCFailure("pmc_asset_hash_invalid")
    if expected_md5 and digest.casefold() != expected_md5.casefold():
        raise PMCFailure("pmc_asset_hash_mismatch")
    root.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination, digest
