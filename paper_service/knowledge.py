"""Canonical reviewed-paper knowledge and deterministic Markdown rendering.

The server and the Mac-side importer share this module so that an outbox item
has one strict representation.  Validation is intentionally independent from
the database and from an Obsidian vault.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import html
import json
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any
from uuid import UUID

from .selectors import SELECTOR_VERSION


KNOWLEDGE_SCHEMA_VERSION = 1
KNOWLEDGE_STATUSES = frozenset({"ai-draft", "reviewed"})

_TOP_LEVEL_REQUIRED = frozenset(
    {
        "schema_version",
        "selector_version",
        "revision_id",
        "status",
        "paper_identity",
        "source_sha256",
        "article_fingerprint",
        "summary",
        "claims",
        "key_methods",
        "key_findings",
        "limitations",
    }
)
_TOP_LEVEL_OPTIONAL = frozenset({"user_annotation_ids"})
_IDENTITY_REQUIRED = frozenset({"paper_id", "title"})
_IDENTITY_OPTIONAL = frozenset({"doi", "authors", "year"})
_CLAIM_KEYS = frozenset({"text", "evidence"})
_EVIDENCE_KEYS = frozenset({"node_id", "exact_quote"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NODE_ID_RE = re.compile(r"n-[0-9a-f]{24}(?:-(?:[2-9]|[1-9][0-9]+))?")
_PAPER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_FILENAME_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f\x7f]")


class KnowledgeValidationError(ValueError):
    """The canonical knowledge payload is malformed or unsupported."""


def _error(code: str) -> KnowledgeValidationError:
    return KnowledgeValidationError(code)


def _enforce_payload_budget(payload: Any) -> None:
    """Bound direct Python callers before validation allocates canonical copies."""
    stack = [payload]
    seen_containers: set[int] = set()
    characters = 0
    nodes = 0
    while stack:
        value = stack.pop()
        nodes += 1
        if nodes > 100_000:
            raise _error("payload_too_many_nodes")
        if isinstance(value, str):
            characters += len(value)
        elif isinstance(value, dict):
            identity = id(value)
            if identity in seen_containers:
                raise _error("payload_alias_or_cycle")
            seen_containers.add(identity)
            for key, item in value.items():
                if isinstance(key, str):
                    characters += len(key)
                stack.append(item)
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_containers:
                raise _error("payload_alias_or_cycle")
            seen_containers.add(identity)
            stack.extend(value)
        if characters > 2_000_000:
            raise _error("payload_too_large")


def _object(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"{field}_must_be_object")
    if any(not isinstance(key, str) for key in value):
        raise _error(f"{field}_key_invalid")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise _error(f"{field}_missing_{sorted(missing)[0]}")
    if unknown:
        raise _error(f"{field}_unknown_field")
    return value


def _text(value: Any, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _error(f"{field}_must_be_string")
    result = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    result = result.strip()
    if not result:
        raise _error(f"{field}_empty")
    if len(result) > limit:
        raise _error(f"{field}_too_long")
    if _CONTROL_RE.search(result):
        raise _error(f"{field}_control_character")
    return result


def _optional_text(value: Any, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _text(value, field=field, limit=limit)


def _single_line_text(value: Any, *, field: str, limit: int) -> str:
    result = _text(value, field=field, limit=limit)
    if "\n" in result or "\t" in result:
        raise _error(f"{field}_must_be_single_line")
    return result


def _canonical_uuid(value: Any, *, field: str) -> str:
    text = _text(value, field=field, limit=36)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as exc:
        raise _error(f"{field}_invalid_uuid") from exc
    if parsed.int == 0 or str(parsed) != text:
        raise _error(f"{field}_invalid_uuid")
    return text


def _string_list(
    value: Any,
    *,
    field: str,
    item_limit: int = 4_000,
    list_limit: int = 200,
    single_line: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise _error(f"{field}_must_be_array")
    if len(value) > list_limit:
        raise _error(f"{field}_too_many_items")
    validator = _single_line_text if single_line else _text
    result = [validator(item, field=f"{field}_item", limit=item_limit) for item in value]
    if len(result) != len(set(result)):
        raise _error(f"{field}_duplicate_item")
    return result


def _canonical_paper_id(value: Any) -> int | str:
    if isinstance(value, bool):
        raise _error("paper_identity_paper_id_invalid")
    if isinstance(value, int):
        if value <= 0 or value > 9_223_372_036_854_775_807:
            raise _error("paper_identity_paper_id_invalid")
        return value
    if isinstance(value, str) and _PAPER_ID_RE.fullmatch(value):
        return value
    raise _error("paper_identity_paper_id_invalid")


def _canonical_identity(value: Any) -> dict[str, Any]:
    identity = _object(
        value,
        field="paper_identity",
        required=_IDENTITY_REQUIRED,
        optional=_IDENTITY_OPTIONAL,
    )
    result: dict[str, Any] = {
        "paper_id": _canonical_paper_id(identity["paper_id"]),
        "title": _single_line_text(
            identity["title"], field="paper_identity_title", limit=1_000
        ),
    }
    if "doi" in identity:
        doi = _optional_text(identity["doi"], field="paper_identity_doi", limit=512)
        if doi is not None and ("\n" in doi or "\t" in doi):
            raise _error("paper_identity_doi_must_be_single_line")
        if doi is not None:
            result["doi"] = doi
    if "authors" in identity:
        result["authors"] = _string_list(
            identity["authors"],
            field="paper_identity_authors",
            item_limit=500,
            list_limit=500,
            single_line=True,
        )
    if "year" in identity:
        year = identity["year"]
        if year is not None and (
            isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999
        ):
            raise _error("paper_identity_year_invalid")
        if year is not None:
            result["year"] = year
    return result


def _source_text(source_nodes: Mapping[str, Any], node_id: str) -> str | None:
    if node_id not in source_nodes:
        return None
    value = source_nodes[node_id]
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("text"), str):
        return value["text"]
    raise _error("source_node_invalid")


def validate_knowledge_payload(
    payload: Any,
    *,
    source_nodes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a canonical copy of a strict paper-knowledge payload.

    Every claim must carry at least one content-node ID and an exact quote.  If
    ``source_nodes`` is supplied by the server, validation additionally proves
    that each node exists and that the quote occurs verbatim in that node.
    """
    _enforce_payload_budget(payload)
    top = _object(
        payload,
        field="payload",
        required=_TOP_LEVEL_REQUIRED,
        optional=_TOP_LEVEL_OPTIONAL,
    )
    if (
        isinstance(top["schema_version"], bool)
        or not isinstance(top["schema_version"], int)
        or top["schema_version"] != KNOWLEDGE_SCHEMA_VERSION
    ):
        raise _error("unsupported_schema_version")
    if (
        isinstance(top["selector_version"], bool)
        or not isinstance(top["selector_version"], int)
        or top["selector_version"] != SELECTOR_VERSION
    ):
        raise _error("unsupported_selector_version")
    revision_id = _canonical_uuid(top["revision_id"], field="revision_id")
    if not isinstance(top["status"], str) or top["status"] not in KNOWLEDGE_STATUSES:
        raise _error("status_invalid")
    if source_nodes is not None and not isinstance(source_nodes, Mapping):
        raise _error("source_nodes_invalid")
    source_sha256 = _text(top["source_sha256"], field="source_sha256", limit=64)
    if not _SHA256_RE.fullmatch(source_sha256):
        raise _error("source_sha256_invalid")
    article_fingerprint = _text(
        top["article_fingerprint"], field="article_fingerprint", limit=64
    )
    if not _SHA256_RE.fullmatch(article_fingerprint):
        raise _error("article_fingerprint_invalid")

    claims_value = top["claims"]
    if not isinstance(claims_value, list):
        raise _error("claims_must_be_array")
    if len(claims_value) > 500:
        raise _error("claims_too_many_items")
    claims: list[dict[str, Any]] = []
    claim_texts: set[str] = set()
    for claim_index, raw_claim in enumerate(claims_value):
        claim = _object(
            raw_claim,
            field=f"claim_{claim_index}",
            required=_CLAIM_KEYS,
        )
        claim_text = _text(
            claim["text"], field=f"claim_{claim_index}_text", limit=10_000
        )
        if claim_text in claim_texts:
            raise _error("claims_duplicate_text")
        claim_texts.add(claim_text)
        raw_evidence = claim["evidence"]
        if not isinstance(raw_evidence, list):
            raise _error(f"claim_{claim_index}_evidence_must_be_array")
        if not raw_evidence:
            raise _error(f"claim_{claim_index}_unsupported")
        if len(raw_evidence) > 50:
            raise _error(f"claim_{claim_index}_too_much_evidence")
        evidence: list[dict[str, str]] = []
        evidence_keys: set[tuple[str, str]] = set()
        for evidence_index, raw_item in enumerate(raw_evidence):
            item = _object(
                raw_item,
                field=f"claim_{claim_index}_evidence_{evidence_index}",
                required=_EVIDENCE_KEYS,
            )
            node_id = _text(
                item["node_id"],
                field=f"claim_{claim_index}_evidence_{evidence_index}_node_id",
                limit=64,
            )
            if not _NODE_ID_RE.fullmatch(node_id):
                raise _error(f"claim_{claim_index}_evidence_node_id_invalid")
            quote = _text(
                item["exact_quote"],
                field=f"claim_{claim_index}_evidence_{evidence_index}_exact_quote",
                limit=4_000,
            )
            evidence_key = (node_id, quote)
            if evidence_key in evidence_keys:
                raise _error(f"claim_{claim_index}_duplicate_evidence")
            evidence_keys.add(evidence_key)
            if source_nodes is not None:
                source_text = _source_text(source_nodes, node_id)
                if source_text is None:
                    raise _error(f"claim_{claim_index}_evidence_node_missing")
                if quote not in source_text:
                    raise _error(f"claim_{claim_index}_evidence_quote_mismatch")
            evidence.append({"node_id": node_id, "exact_quote": quote})
        claims.append({"text": claim_text, "evidence": evidence})

    canonical: dict[str, Any] = {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "selector_version": SELECTOR_VERSION,
        "revision_id": revision_id,
        "status": top["status"],
        "paper_identity": _canonical_identity(top["paper_identity"]),
        "source_sha256": source_sha256,
        "article_fingerprint": article_fingerprint,
        "summary": _text(top["summary"], field="summary", limit=30_000),
        "claims": claims,
        "key_methods": _string_list(top["key_methods"], field="key_methods"),
        "key_findings": _string_list(top["key_findings"], field="key_findings"),
        "limitations": _string_list(top["limitations"], field="limitations"),
    }
    if "user_annotation_ids" in top:
        raw_ids = top["user_annotation_ids"]
        if not isinstance(raw_ids, list):
            raise _error("user_annotation_ids_must_be_array")
        if len(raw_ids) > 1_000:
            raise _error("user_annotation_ids_too_many_items")
        annotation_ids = [
            _canonical_uuid(item, field="user_annotation_id") for item in raw_ids
        ]
        if len(annotation_ids) != len(set(annotation_ids)):
            raise _error("user_annotation_ids_duplicate_item")
        canonical["user_annotation_ids"] = annotation_ids
    return canonical


def canonical_payload_bytes(payload: Any) -> bytes:
    """Serialize a validated payload with a stable byte representation."""
    canonical = validate_knowledge_payload(payload)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error("json_duplicate_key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise KnowledgeValidationError(f"json_non_finite_{value}")


def _parse_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 19:
        raise KnowledgeValidationError("json_integer_too_long")
    return int(value)


def _reject_json_float(value: str) -> None:
    raise KnowledgeValidationError("json_float_unsupported")


def _reject_excessive_json_depth(value: Any, *, maximum: int = 64) -> None:
    """Bound nesting independently of interpreter-specific JSON recursion."""

    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > maximum:
            raise KnowledgeValidationError("payload_json_too_deep")
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth + 1) for child in children)


def load_knowledge_payload(path: str | Path) -> dict[str, Any]:
    """Load a bounded canonical JSON file and reject ambiguous duplicate keys."""
    source = Path(path)
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError as exc:
        raise KnowledgeValidationError("payload_file_invalid") from exc
    if not stat.S_ISREG(mode):
        raise KnowledgeValidationError("payload_file_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise KnowledgeValidationError("payload_file_invalid") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise KnowledgeValidationError("payload_file_invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 2_000_000:
                raise KnowledgeValidationError("payload_file_too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer,
            parse_float=_reject_json_float,
        )
    except RecursionError as exc:
        raise KnowledgeValidationError("payload_json_too_deep") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeValidationError("payload_json_invalid") from exc
    _reject_excessive_json_depth(decoded)
    return validate_knowledge_payload(decoded)


def safe_knowledge_filename(payload: Any) -> str:
    """Return a cross-platform, deterministic filename for one paper note."""
    canonical = validate_knowledge_payload(payload)
    identity = canonical["paper_identity"]
    typed_id = [
        "integer" if isinstance(identity["paper_id"], int) else "string",
        identity["paper_id"],
    ]
    namespace = hashlib.sha256(
        json.dumps(typed_id, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    display_id = _UNSAFE_FILENAME_RE.sub("-", str(identity["paper_id"]))
    filename = f"Paper {display_id} -- {namespace}.md"
    if filename in {".", ".."} or len(filename.encode("utf-8")) > 180:
        raise KnowledgeValidationError("paper_filename_invalid")
    return filename


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _markdown_text(value: str) -> str:
    escaped = html.escape(value, quote=False).replace("\\", "\\\\")
    for marker in ("`", "*", "_", "[", "]", "#", "~", "|", "^", "$", "%"):
        escaped = escaped.replace(marker, f"\\{marker}")
    lines: list[str] = []
    for line in escaped.split("\n"):
        if re.match(r"\s*(?:#{1,6}|>|-{3,}|[-+]|\d+[.)])(?:\s|$)", line):
            line = "\\" + line
        lines.append(line)
    return "  \n".join(lines)


def _bullet_section(title: str, values: list[str]) -> list[str]:
    result = [f"## {title}", ""]
    if values:
        result.extend(f"- {_markdown_text(item)}" for item in values)
    else:
        result.append("_None recorded._")
    result.append("")
    return result


def render_knowledge_markdown(payload: Any) -> str:
    """Render canonical knowledge using a fixed, injection-resistant template."""
    item = validate_knowledge_payload(payload)
    identity = item["paper_identity"]
    canonical_digest = payload_sha256(item)
    rendered_paper_id = (
        str(identity["paper_id"])
        if isinstance(identity["paper_id"], int)
        else _yaml_string(identity["paper_id"])
    )
    lines = [
        "---",
        f"schema_version: {KNOWLEDGE_SCHEMA_VERSION}",
        f"selector_version: {item['selector_version']}",
        f"knowledge_revision_id: {_yaml_string(item['revision_id'])}",
        f"canonical_payload_sha256: {_yaml_string(canonical_digest)}",
        f"status: {_yaml_string(item['status'])}",
        f"paper_id: {rendered_paper_id}",
        f"source_sha256: {_yaml_string(item['source_sha256'])}",
        f"article_fingerprint: {_yaml_string(item['article_fingerprint'])}",
    ]
    if identity.get("doi"):
        lines.append(f"doi: {_yaml_string(identity['doi'])}")
    if identity.get("year") is not None:
        lines.append(f"year: {identity['year']}")
    if "authors" in identity:
        if identity["authors"]:
            lines.append("authors:")
            lines.extend(
                f"  - {_yaml_string(author)}" for author in identity["authors"]
            )
        else:
            lines.append("authors: []")
    lines.extend(
        [
            "---",
            "",
            f"# {_markdown_text(identity['title'])}",
            "",
        ]
    )
    if item["status"] == "reviewed":
        lines.extend(
            [
                "> [!success] Reviewed knowledge",
                "> This revision was approved for durable use.",
            ]
        )
    else:
        lines.extend(
            [
                "> [!warning] AI draft — not a source of truth",
                "> Verify every claim against its cited source before promotion.",
            ]
        )
    lines.extend(
        [
            "",
            "## Source identity",
            "",
            f"- Source PDF SHA-256: `{item['source_sha256']}`",
            f"- Article fingerprint: `{item['article_fingerprint']}`",
            f"- Selector version: `{item['selector_version']}`",
            f"- Knowledge revision: `{item['revision_id']}`",
            f"- Canonical payload SHA-256: `{canonical_digest}`",
        ]
    )
    if identity.get("doi"):
        lines.append(f"- DOI: {_markdown_text(identity['doi'])}")
    lines.extend(["", "## Summary", "", _markdown_text(item["summary"]), ""])

    lines.extend(["## Claims and evidence", ""])
    if not item["claims"]:
        lines.extend(["_No supported claims recorded._", ""])
    for index, claim in enumerate(item["claims"], start=1):
        lines.extend(
            [
                f"### Claim {index}",
                "",
                _markdown_text(claim["text"]),
                "",
                "Evidence:",
                "",
            ]
        )
        for evidence in claim["evidence"]:
            quote = _markdown_text(evidence["exact_quote"])
            lines.extend(
                [
                    f"- Source node `{evidence['node_id']}`",
                    f"  - Exact quote: “{quote}”",
                ]
            )
        lines.append("")

    lines.extend(_bullet_section("Key methods", item["key_methods"]))
    lines.extend(_bullet_section("Key findings", item["key_findings"]))
    lines.extend(_bullet_section("Limitations", item["limitations"]))
    if "user_annotation_ids" in item:
        lines.extend(["## Linked user annotations", ""])
        if item["user_annotation_ids"]:
            lines.extend(f"- `{value}`" for value in item["user_annotation_ids"])
        else:
            lines.append("_None linked._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "KNOWLEDGE_SCHEMA_VERSION",
    "KNOWLEDGE_STATUSES",
    "KnowledgeValidationError",
    "canonical_payload_bytes",
    "load_knowledge_payload",
    "payload_sha256",
    "render_knowledge_markdown",
    "safe_knowledge_filename",
    "validate_knowledge_payload",
]
