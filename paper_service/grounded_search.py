"""Deterministic, authority-aware primitives for grounded paper search.

This module is deliberately storage- and provider-neutral.  It prepares plain
text records for a future lexical/FTS index, gives every chunk a stable ID, and
keeps unreviewed AI knowledge out of the searchable corpus.  Callers remain
responsible for obtaining the source records from the current system of record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from .selectors import SELECTOR_VERSION


SEARCH_SCHEMA_VERSION = 1
DEFAULT_CHUNK_CHARS = 1_400
DEFAULT_CHUNK_OVERLAP = 120
MAX_DOCUMENT_CHARS = 100_000
MAX_DOCUMENTS = 10_000
MAX_CORPUS_CHARACTERS = 2_000_000
MAX_SEARCH_CHUNKS = 100_000
MAX_QUERY_CHARS = 1_000
MAX_QUERY_TERMS = 64
MAX_SEARCH_LIMIT = 100

_DOCUMENT_REQUIRED = frozenset(
    {
        "schema_version",
        "selector_version",
        "source_kind",
        "source_id",
        "node_id",
        "text",
    }
)
_NODE_ID_RE = re.compile(r"n-[0-9a-f]{24}(?:-(?:[2-9]|[1-9][0-9]+))?")
_TERM_RE = re.compile(r"[^\W_]+(?:['_-][^\W_]+)*", re.UNICODE)
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


class GroundedSearchError(ValueError):
    """A search record or query violates the grounded-search contract."""


class SourceKind(str, Enum):
    """Typed provenance used by both retrieval and answer citations."""

    USER_ANNOTATION = "user_annotation"
    REVIEWED_KNOWLEDGE = "reviewed_knowledge"
    SOURCE_TEXT = "source_text"
    AI_DRAFT_KNOWLEDGE = "ai_draft_knowledge"


class AuthorityScore(IntEnum):
    """Non-overlapping authority bands; lexical score only breaks local ties."""

    EXCLUDED = 0
    SOURCE_TEXT = 100
    REVIEWED_KNOWLEDGE = 200
    USER_ANNOTATION = 300


SOURCE_AUTHORITY = MappingProxyType(
    {
        SourceKind.USER_ANNOTATION: AuthorityScore.USER_ANNOTATION,
        SourceKind.REVIEWED_KNOWLEDGE: AuthorityScore.REVIEWED_KNOWLEDGE,
        SourceKind.SOURCE_TEXT: AuthorityScore.SOURCE_TEXT,
        SourceKind.AI_DRAFT_KNOWLEDGE: AuthorityScore.EXCLUDED,
    }
)
SEARCHABLE_SOURCE_KINDS = frozenset(
    {
        SourceKind.USER_ANNOTATION,
        SourceKind.REVIEWED_KNOWLEDGE,
        SourceKind.SOURCE_TEXT,
    }
)


def _fail(code: str) -> GroundedSearchError:
    return GroundedSearchError(code)


def _object(value: Any, *, field: str, required: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{field}_must_be_object")
    if any(not isinstance(key, str) for key in value):
        raise _fail(f"{field}_key_invalid")
    missing = required - set(value)
    unknown = set(value) - required
    if missing:
        raise _fail(f"{field}_missing_{sorted(missing)[0]}")
    if unknown:
        raise _fail(f"{field}_unknown_field")
    return value


def _contains_unsafe_control(value: str, *, allow_newlines: bool) -> bool:
    for character in value:
        codepoint = ord(character)
        if codepoint in _BIDI_CONTROL_CODEPOINTS:
            return True
        category = unicodedata.category(character)
        if category == "Cs":
            return True
        if category == "Cc" and not (allow_newlines and character in "\n\t"):
            return True
    return False


def canonical_plain_text(value: Any, *, field: str, limit: int) -> str:
    """Normalize safe plain text without changing quote-significant spacing."""

    if not isinstance(value, str):
        raise _fail(f"{field}_must_be_string")
    result = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n"),
    ).strip()
    if not result:
        raise _fail(f"{field}_empty")
    if len(result) > limit:
        raise _fail(f"{field}_too_long")
    if _contains_unsafe_control(result, allow_newlines=True):
        raise _fail(f"{field}_unsafe_control")
    return result


def canonical_identifier(value: Any, *, field: str, limit: int = 512) -> str:
    """Return a bounded, single-line NFC identifier suitable for JSON keys."""

    result = canonical_plain_text(value, field=field, limit=limit)
    if "\n" in result or "\t" in result:
        raise _fail(f"{field}_must_be_single_line")
    return result


def normalize_lexical_text(value: Any) -> str:
    """Return the deterministic Unicode form stored in a lexical/FTS column."""

    text = canonical_plain_text(value, field="text", limit=MAX_DOCUMENT_CHARS)
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split())


def lexical_terms(value: Any) -> tuple[str, ...]:
    """Tokenize a query deterministically while retaining first-seen order."""

    normalized = normalize_lexical_text(value)
    terms: list[str] = []
    seen: set[str] = set()
    for term in _TERM_RE.findall(normalized):
        if term not in seen:
            terms.append(term)
            seen.add(term)
        if len(terms) > MAX_QUERY_TERMS:
            raise _fail("query_too_many_terms")
    if not terms:
        raise _fail("query_has_no_lexical_terms")
    return tuple(terms)


def parse_source_kind(value: Any) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    if not isinstance(value, str):
        raise _fail("source_kind_invalid")
    try:
        return SourceKind(value)
    except ValueError as exc:
        raise _fail("source_kind_invalid") from exc


def source_kind_for_knowledge_status(status: Any) -> SourceKind:
    """Map the existing knowledge review state without treating drafts as reviewed."""

    if status == "reviewed":
        return SourceKind.REVIEWED_KNOWLEDGE
    if status == "ai-draft":
        return SourceKind.AI_DRAFT_KNOWLEDGE
    raise _fail("knowledge_status_invalid")


def authority_score(source_kind: Any) -> AuthorityScore:
    return SOURCE_AUTHORITY[parse_source_kind(source_kind)]


def is_searchable_source(source_kind: Any) -> bool:
    return parse_source_kind(source_kind) in SEARCHABLE_SOURCE_KINDS


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_id(prefix: str, kind: str, value: Any, length: int = 32) -> str:
    digest = hashlib.sha256(
        _canonical_json_bytes(
            {
                "kind": kind,
                "schema_version": SEARCH_SCHEMA_VERSION,
                "selector_version": SELECTOR_VERSION,
                "value": value,
            }
        )
    ).hexdigest()[:length]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class NormalizedSearchDocument:
    schema_version: int
    selector_version: int
    document_id: str
    source_kind: SourceKind
    source_id: str
    node_id: str
    text: str
    lexical_text: str
    authority_score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selector_version": self.selector_version,
            "document_id": self.document_id,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "node_id": self.node_id,
            "text": self.text,
            "lexical_text": self.lexical_text,
            "authority_score": self.authority_score,
        }

    def to_source_node(self) -> dict[str, Any]:
        """Return the minimal typed node accepted by the grounded-QA contract."""

        return {
            "selector_version": self.selector_version,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "node_id": self.node_id,
            "text": self.text,
        }


@dataclass(frozen=True)
class SearchChunk:
    schema_version: int
    selector_version: int
    chunk_id: str
    document_id: str
    source_kind: SourceKind
    source_id: str
    node_id: str
    start: int
    end: int
    text: str
    lexical_text: str
    authority_score: int

    def to_fts_row(self) -> dict[str, Any]:
        """Return only deterministic, index-safe scalar fields."""

        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "selector_version": self.selector_version,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "node_id": self.node_id,
            "authority_score": self.authority_score,
            "content": self.lexical_text,
        }


@dataclass(frozen=True)
class SearchHit:
    chunk: SearchChunk
    lexical_score: int
    authority_score: int
    total_score: int
    paper_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_fts_row(),
            "text": self.chunk.text,
            "lexical_score": self.lexical_score,
            "authority_score": self.authority_score,
            "total_score": self.total_score,
            "paper_id": self.paper_id,
        }


def normalize_search_document(
    value: Mapping[str, Any],
) -> NormalizedSearchDocument | None:
    """Validate one strict record, returning ``None`` for an AI draft.

    The explicit ``None`` is the only successful representation of an
    ``ai_draft_knowledge`` record.  It cannot accidentally enter chunking or
    ranking as a zero-scored document.
    """

    record = _object(value, field="document", required=_DOCUMENT_REQUIRED)
    if (
        isinstance(record["schema_version"], bool)
        or not isinstance(record["schema_version"], int)
        or record["schema_version"] != SEARCH_SCHEMA_VERSION
    ):
        raise _fail("unsupported_search_schema_version")
    if (
        isinstance(record["selector_version"], bool)
        or not isinstance(record["selector_version"], int)
        or record["selector_version"] != SELECTOR_VERSION
    ):
        raise _fail("unsupported_selector_version")
    source_kind = parse_source_kind(record["source_kind"])
    source_id = canonical_identifier(record["source_id"], field="source_id")
    node_id = canonical_identifier(record["node_id"], field="node_id", limit=64)
    if not _NODE_ID_RE.fullmatch(node_id):
        raise _fail("node_id_invalid")
    text = canonical_plain_text(record["text"], field="text", limit=MAX_DOCUMENT_CHARS)
    if source_kind == SourceKind.AI_DRAFT_KNOWLEDGE:
        return None
    lexical_text = normalize_lexical_text(text)
    document_id = _stable_id(
        "d",
        "search_document",
        [source_kind.value, source_id, node_id, text],
    )
    return NormalizedSearchDocument(
        schema_version=SEARCH_SCHEMA_VERSION,
        selector_version=SELECTOR_VERSION,
        document_id=document_id,
        source_kind=source_kind,
        source_id=source_id,
        node_id=node_id,
        text=text,
        lexical_text=lexical_text,
        authority_score=int(authority_score(source_kind)),
    )


def normalize_search_documents(
    values: Iterable[Mapping[str, Any]],
) -> tuple[NormalizedSearchDocument, ...]:
    """Canonicalize, de-duplicate, and order a bounded corpus."""

    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        raise _fail("documents_must_be_iterable")
    output: list[NormalizedSearchDocument] = []
    seen_ids: set[str] = set()
    seen_nodes: set[tuple[SourceKind, str, str]] = set()
    count = 0
    total_characters = 0
    for value in values:
        count += 1
        if count > MAX_DOCUMENTS:
            raise _fail("documents_too_many_items")
        document = normalize_search_document(value)
        if document is None:
            continue
        total_characters += len(document.text)
        if total_characters > MAX_CORPUS_CHARACTERS:
            raise _fail("documents_character_budget_exceeded")
        node_key = (document.source_kind, document.source_id, document.node_id)
        if document.document_id in seen_ids or node_key in seen_nodes:
            raise _fail("duplicate_search_document")
        seen_ids.add(document.document_id)
        seen_nodes.add(node_key)
        output.append(document)
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.source_kind.value,
                item.source_id,
                item.node_id,
                item.document_id,
            ),
        )
    )


def _chunk_spans(text: str, *, chunk_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    length = len(text)
    while start < length:
        hard_end = min(length, start + chunk_chars)
        end = hard_end
        if hard_end < length:
            lower_bound = start + max(1, chunk_chars // 2)
            paragraph_break = text.rfind("\n\n", lower_bound, hard_end + 1)
            whitespace_break = max(
                text.rfind(" ", lower_bound, hard_end + 1),
                text.rfind("\n", lower_bound, hard_end + 1),
                text.rfind("\t", lower_bound, hard_end + 1),
            )
            if paragraph_break >= lower_bound:
                end = paragraph_break
            elif whitespace_break >= lower_bound:
                end = whitespace_break
        segment_start = start
        segment_end = end
        while segment_start < segment_end and text[segment_start].isspace():
            segment_start += 1
        while segment_end > segment_start and text[segment_end - 1].isspace():
            segment_end -= 1
        if segment_start < segment_end:
            spans.append((segment_start, segment_end))
        if hard_end >= length:
            break
        next_start = max(start + 1, end - overlap_chars)
        if overlap_chars and next_start < end:
            while next_start < end and not text[next_start - 1].isspace():
                next_start += 1
        if next_start >= end:
            next_start = end
        start = next_start
    return spans


def chunk_search_document(
    document: NormalizedSearchDocument,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[SearchChunk, ...]:
    """Split a normalized node deterministically and derive stable chunk IDs."""

    if not isinstance(document, NormalizedSearchDocument):
        raise _fail("document_not_normalized")
    if document.schema_version != SEARCH_SCHEMA_VERSION:
        raise _fail("unsupported_search_schema_version")
    if document.selector_version != SELECTOR_VERSION:
        raise _fail("unsupported_selector_version")
    source_kind = parse_source_kind(document.source_kind)
    if source_kind not in SEARCHABLE_SOURCE_KINDS:
        raise _fail("source_kind_not_searchable")
    source_id = canonical_identifier(document.source_id, field="source_id")
    node_id = canonical_identifier(document.node_id, field="node_id", limit=64)
    if source_id != document.source_id or not _NODE_ID_RE.fullmatch(node_id):
        raise _fail("document_identity_invalid")
    text = canonical_plain_text(
        document.text, field="text", limit=MAX_DOCUMENT_CHARS
    )
    lexical_text = normalize_lexical_text(text)
    expected_document_id = _stable_id(
        "d", "search_document", [source_kind.value, source_id, node_id, text]
    )
    if (
        document.document_id != expected_document_id
        or document.lexical_text != lexical_text
    ):
        raise _fail("document_normalization_mismatch")
    if document.authority_score != int(authority_score(source_kind)):
        raise _fail("authority_score_mismatch")
    if (
        isinstance(chunk_chars, bool)
        or not isinstance(chunk_chars, int)
        or not 128 <= chunk_chars <= 8_000
    ):
        raise _fail("chunk_chars_invalid")
    if (
        isinstance(overlap_chars, bool)
        or not isinstance(overlap_chars, int)
        or overlap_chars < 0
        or overlap_chars >= chunk_chars // 2
    ):
        raise _fail("overlap_chars_invalid")
    chunks: list[SearchChunk] = []
    for start, end in _chunk_spans(
        document.text, chunk_chars=chunk_chars, overlap_chars=overlap_chars
    ):
        text = document.text[start:end]
        chunk_id = _stable_id(
            "c",
            "search_chunk",
            [
                document.document_id,
                document.source_kind.value,
                document.source_id,
                document.node_id,
                start,
                end,
                text,
            ],
        )
        chunks.append(
            SearchChunk(
                schema_version=SEARCH_SCHEMA_VERSION,
                selector_version=SELECTOR_VERSION,
                chunk_id=chunk_id,
                document_id=document.document_id,
                source_kind=document.source_kind,
                source_id=document.source_id,
                node_id=document.node_id,
                start=start,
                end=end,
                text=text,
                lexical_text=normalize_lexical_text(text),
                authority_score=document.authority_score,
            )
        )
    return tuple(chunks)


def build_search_chunks(
    values: Iterable[Mapping[str, Any]],
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[SearchChunk, ...]:
    documents = normalize_search_documents(values)
    output: list[SearchChunk] = []
    for document in documents:
        output.extend(
            chunk_search_document(
                document,
                chunk_chars=chunk_chars,
                overlap_chars=overlap_chars,
            )
        )
        if len(output) > MAX_SEARCH_CHUNKS:
            raise _fail("chunks_too_many_items")
    return tuple(output)


def _lexical_score(normalized_query: str, terms: tuple[str, ...], text: str) -> int:
    phrase_count = text.count(normalized_query)
    term_counts = [text.count(term) for term in terms]
    matched = sum(count > 0 for count in term_counts)
    if not phrase_count and not matched:
        return 0
    coverage = (matched * 1_000) // len(terms)
    frequency = sum(min(count, 100) for count in term_counts)
    return min(999_999, phrase_count * 10_000 + coverage * 100 + frequency * 10)


def search_chunks(
    query: Any,
    chunks: Iterable[SearchChunk],
    *,
    limit: int = 20,
) -> tuple[SearchHit, ...]:
    """Rank matching chunks by strict provenance authority, then lexical score."""

    query_text = canonical_plain_text(query, field="query", limit=MAX_QUERY_CHARS)
    normalized_query = unicodedata.normalize("NFKC", query_text).casefold()
    normalized_query = " ".join(normalized_query.split())
    terms = lexical_terms(query_text)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_SEARCH_LIMIT
    ):
        raise _fail("search_limit_invalid")
    if not isinstance(chunks, Iterable) or isinstance(chunks, (str, bytes, Mapping)):
        raise _fail("chunks_must_be_iterable")
    hits: list[SearchHit] = []
    seen_chunks: set[str] = set()
    chunk_count = 0
    for chunk in chunks:
        chunk_count += 1
        if chunk_count > MAX_SEARCH_CHUNKS:
            raise _fail("chunks_too_many_items")
        if not isinstance(chunk, SearchChunk):
            raise _fail("chunk_invalid")
        chunk_id = canonical_identifier(chunk.chunk_id, field="chunk_id", limit=34)
        if not re.fullmatch(r"c-[0-9a-f]{32}", chunk_id):
            raise _fail("chunk_id_invalid")
        if chunk_id in seen_chunks:
            raise _fail("duplicate_chunk")
        seen_chunks.add(chunk_id)
        if chunk.schema_version != SEARCH_SCHEMA_VERSION:
            raise _fail("unsupported_search_schema_version")
        if chunk.selector_version != SELECTOR_VERSION:
            raise _fail("unsupported_selector_version")
        source_kind = parse_source_kind(chunk.source_kind)
        if source_kind not in SEARCHABLE_SOURCE_KINDS:
            continue
        source_id = canonical_identifier(chunk.source_id, field="source_id")
        node_id = canonical_identifier(chunk.node_id, field="node_id", limit=64)
        if source_id != chunk.source_id or not _NODE_ID_RE.fullmatch(node_id):
            raise _fail("chunk_identity_invalid")
        text = canonical_plain_text(chunk.text, field="chunk_text", limit=8_000)
        if (
            isinstance(chunk.start, bool)
            or not isinstance(chunk.start, int)
            or isinstance(chunk.end, bool)
            or not isinstance(chunk.end, int)
            or chunk.start < 0
            or chunk.end <= chunk.start
            or chunk.end - chunk.start != len(text)
        ):
            raise _fail("chunk_span_invalid")
        lexical_text = normalize_lexical_text(text)
        if text != chunk.text or lexical_text != chunk.lexical_text:
            raise _fail("chunk_normalization_mismatch")
        document_id = canonical_identifier(
            chunk.document_id, field="document_id", limit=34
        )
        if not re.fullmatch(r"d-[0-9a-f]{32}", document_id):
            raise _fail("document_id_invalid")
        expected_chunk_id = _stable_id(
            "c",
            "search_chunk",
            [
                document_id,
                source_kind.value,
                source_id,
                node_id,
                chunk.start,
                chunk.end,
                text,
            ],
        )
        if chunk_id != expected_chunk_id:
            raise _fail("chunk_id_mismatch")
        expected_authority = int(authority_score(source_kind))
        if chunk.authority_score != expected_authority:
            raise _fail("authority_score_mismatch")
        score = _lexical_score(normalized_query, terms, chunk.lexical_text)
        if not score:
            continue
        # One authority point is wider than the complete lexical band, making
        # USER > REVIEWED > SOURCE a guaranteed invariant rather than a hint.
        total_score = expected_authority * 1_000_000 + score
        hits.append(SearchHit(chunk, score, expected_authority, total_score))
    hits.sort(
        key=lambda hit: (
            -hit.authority_score,
            -hit.lexical_score,
            hit.chunk.source_kind.value,
            hit.chunk.source_id,
            hit.chunk.node_id,
            hit.chunk.chunk_id,
        )
    )
    return tuple(hits[:limit])


def escape_plain_text_for_html(value: Any) -> str:
    """Escape validated plain text for an HTML-only presentation boundary."""

    text = canonical_plain_text(value, field="text", limit=MAX_DOCUMENT_CHARS)
    return html.escape(text, quote=True)
