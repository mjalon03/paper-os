"""Strict request and result contract for the egress-only PMC broker."""

from __future__ import annotations

from typing import Any, Mapping
import re

from .pmc import PMCFailure, normalize_doi
from .reader_v2_job import (
    canonical_json_bytes,
    clean_conversion_paper,
    payload_sha256,
    validate_file_manifest,
)


PMC_BROKER_SCHEMA = "paper-pmc-broker"
PMC_BROKER_SCHEMA_VERSION = 1
PMC_BROKER_METADATA_NAME = "_pmc-broker.json"
PMC_BROKER_MAX_REQUEST_BYTES = 256 * 1024
PMC_BROKER_MAX_METADATA_BYTES = 256 * 1024
PMC_BROKER_MAX_BUNDLE_BYTES = 258 * 1024 * 1024
_REQUEST_ID_RE = re.compile(r"pmc-[0-9a-f]{32}")


class PMCBrokerContractFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise PMCBrokerContractFailure("pmc_broker_request_id_invalid")
    return value


def build_request(*, identifier: str, paper: Mapping[str, Any]) -> dict[str, Any]:
    clean = clean_conversion_paper(paper)
    try:
        clean["doi"] = normalize_doi(str(clean.get("doi") or ""))
    except PMCFailure as exc:
        raise PMCBrokerContractFailure("pmc_broker_doi_invalid") from exc
    return validate_request(
        {
            "schema": PMC_BROKER_SCHEMA,
            "schema_version": PMC_BROKER_SCHEMA_VERSION,
            "request_id": request_id(identifier),
            "paper": clean,
        }
    )


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schema_version",
        "request_id",
        "paper",
    }:
        raise PMCBrokerContractFailure("pmc_broker_request_invalid")
    if (
        value["schema"] != PMC_BROKER_SCHEMA
        or value["schema_version"] != PMC_BROKER_SCHEMA_VERSION
    ):
        raise PMCBrokerContractFailure("pmc_broker_request_invalid")
    try:
        clean = clean_conversion_paper(value["paper"])
        normalized = normalize_doi(str(clean.get("doi") or ""))
    except (PMCFailure, RuntimeError) as exc:
        raise PMCBrokerContractFailure("pmc_broker_request_invalid") from exc
    if clean.get("doi") != normalized:
        raise PMCBrokerContractFailure("pmc_broker_request_invalid")
    result = {
        "schema": PMC_BROKER_SCHEMA,
        "schema_version": PMC_BROKER_SCHEMA_VERSION,
        "request_id": request_id(value["request_id"]),
        "paper": clean,
    }
    if len(canonical_json_bytes(result)) > PMC_BROKER_MAX_REQUEST_BYTES:
        raise PMCBrokerContractFailure("pmc_broker_request_too_large")
    return result


def result_metadata(
    *,
    identifier: str,
    request_sha256: str,
    files: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(request_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", request_sha256
    ):
        raise PMCBrokerContractFailure("pmc_broker_result_invalid")
    result = {
        "schema": PMC_BROKER_SCHEMA,
        "schema_version": PMC_BROKER_SCHEMA_VERSION,
        "request_id": request_id(identifier),
        "request_sha256": request_sha256,
        "source": "pmc_jats",
        "files": validate_file_manifest(files),
        "status": "ready",
    }
    if len(canonical_json_bytes(result)) > PMC_BROKER_MAX_METADATA_BYTES:
        raise PMCBrokerContractFailure("pmc_broker_result_too_large")
    return result


def validate_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "schema_version",
        "request_id",
        "request_sha256",
        "source",
        "files",
        "status",
    }:
        raise PMCBrokerContractFailure("pmc_broker_result_invalid")
    canonical = result_metadata(
        identifier=value["request_id"],
        request_sha256=value["request_sha256"],
        files=value["files"],
    )
    if dict(value) != canonical:
        raise PMCBrokerContractFailure("pmc_broker_result_invalid")
    return canonical


__all__ = [
    "PMC_BROKER_MAX_BUNDLE_BYTES",
    "PMC_BROKER_MAX_METADATA_BYTES",
    "PMC_BROKER_MAX_REQUEST_BYTES",
    "PMC_BROKER_METADATA_NAME",
    "PMCBrokerContractFailure",
    "build_request",
    "canonical_json_bytes",
    "payload_sha256",
    "request_id",
    "result_metadata",
    "validate_request",
    "validate_result",
]
