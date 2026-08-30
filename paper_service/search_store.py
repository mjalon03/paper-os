"""PostgreSQL storage for immutable grounded-search indexes and QA jobs.

Search generations are immutable.  A per-paper head is the only mutable index
pointer, so a rebuild becomes visible atomically.  QA jobs retain an immutable
evidence snapshot and references to the exact chunks used to build it.  Model
requests are revalidated against the current paper, reviewed knowledge, and
annotation sharing consent immediately before they leave the server.

This module intentionally never logs questions, source text, answers, or model
payloads.  Public errors expose bounded machine codes only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4, uuid5

from .database import connection
from .grounded_search import (
    MAX_QUERY_CHARS,
    MAX_SEARCH_LIMIT,
    SEARCH_SCHEMA_VERSION,
    NormalizedSearchDocument,
    SearchChunk,
    SearchHit,
    SourceKind,
    build_search_chunks,
    canonical_plain_text,
    lexical_terms,
    normalize_search_documents,
    search_chunks,
)
from .knowledge import KnowledgeValidationError, validate_knowledge_payload
from .knowledge_worker import article_source_node_records, article_source_nodes
from .qa_contract import (
    QAContractError,
    build_qa_prompt_payload,
    validate_qa_request,
    validate_qa_response,
)
from .reader_v2 import read_article
from .selectors import SELECTOR_VERSION, build_selector_index


_GENERATION_NAMESPACE = UUID("cc991512-8797-5e64-a85d-f0eb896ac8c4")
_QA_JOB_NAMESPACE = UUID("9f45c9ef-ff71-5eed-a02a-bdfbb0ec172a")
_QA_USAGE_NAMESPACE = UUID("848977b5-1d66-599d-9fcb-44f69f34dc05")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,120}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_DATABASE_CANDIDATES = 10_000


QA_JOB_COLUMNS = """
id, paper_id, idempotency_key_digest, request_digest, question, provider,
model, request_actor_kind, request_actor_id, request_acknowledged,
include_personal_annotations, evidence_limit, generation_id,
source_sha256, article_fingerprint, selector_version, evidence_snapshot,
evidence_snapshot_sha256, status, attempt_count, max_attempts,
next_attempt_at, lease_owner, lease_token, lease_expires_at, answer_payload,
response_digest, last_error_code, requested_at, started_at, finished_at
"""


class SearchStoreError(RuntimeError):
    """A persisted search/QA invariant failed with a safe error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SearchIndexNotReady(SearchStoreError):
    """No current immutable search generation exists for the requested paper."""


class QALeaseLost(SearchStoreError):
    """A QA worker no longer owns a live lease."""


def _fail(code: str) -> SearchStoreError:
    return SearchStoreError(code)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("value_not_canonical_json") from exc


def _json_text(value: Any) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _fail(f"{field}_invalid")
    return value


def _positive_int(value: Any, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(f"{field}_invalid")
    if maximum is not None and value > maximum:
        raise _fail(f"{field}_invalid")
    return value


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"{field}_invalid")
    return value


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field}_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or _CONTROL_RE.search(normalized):
        raise _fail(f"{field}_invalid")
    try:
        normalized.encode("utf-8")
    except UnicodeError as exc:
        raise _fail(f"{field}_invalid") from exc
    return normalized


def _uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, (str, UUID)):
        raise _fail(f"{field}_invalid")
    try:
        parsed = UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise _fail(f"{field}_invalid") from exc
    if parsed.int == 0 or str(parsed) != str(value):
        raise _fail(f"{field}_invalid")
    return str(parsed)


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise _fail(f"{field}_invalid") from exc
    if not isinstance(value, dict):
        raise _fail(f"{field}_invalid")
    return value


def _row(value: Any, *, code: str) -> dict[str, Any]:
    if value is None:
        raise _fail(code)
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise _fail("database_row_invalid") from exc


@dataclass(frozen=True)
class PreparedSearchDocument:
    document: NormalizedSearchDocument
    model_eligible: bool
    reviewed_revision_id: str | None = None
    annotation_id: str | None = None


@dataclass(frozen=True)
class PreparedSearchCorpus:
    paper_id: int
    generation_id: str
    corpus_digest: str
    source_sha256: str
    article_fingerprint: str
    selector_version: int
    documents: tuple[PreparedSearchDocument, ...]
    chunks: tuple[SearchChunk, ...]

    @property
    def model_eligibility(self) -> dict[str, bool]:
        return {
            item.document.document_id: item.model_eligible for item in self.documents
        }

    @property
    def document_origins(self) -> dict[str, tuple[str | None, str | None]]:
        return {
            item.document.document_id: (
                item.reviewed_revision_id,
                item.annotation_id,
            )
            for item in self.documents
        }


@dataclass(frozen=True)
class QAUsage:
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_microusd: int | None = None
    cost_basis: str = "unavailable"
    pricing_version: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.provider, field="usage_provider", maximum=80)
        _bounded_text(self.model, field="usage_model", maximum=200)
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            _nonnegative_int(getattr(self, field), field=f"usage_{field}")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise _fail("usage_total_tokens_mismatch")
        if self.cost_basis not in {
            "unavailable",
            "provider-reported",
            "reviewed-price-table",
        }:
            raise _fail("usage_cost_basis_invalid")
        if self.cost_microusd is not None:
            _nonnegative_int(self.cost_microusd, field="usage_cost_microusd")
        if self.cost_basis == "unavailable":
            if self.cost_microusd is not None or self.pricing_version is not None:
                raise _fail("usage_unavailable_cost_has_value")
        elif self.cost_microusd is None:
            raise _fail("usage_cost_missing")
        if self.cost_basis == "reviewed-price-table":
            _bounded_text(
                self.pricing_version,
                field="usage_pricing_version",
                maximum=120,
            )
        elif self.pricing_version is not None:
            raise _fail("usage_pricing_version_unexpected")


def _annotation_document(
    annotation: Mapping[str, Any],
    *,
    paper_id: int,
    article_fingerprint: str,
    source_nodes: Mapping[str, str],
) -> tuple[dict[str, Any], bool, str] | None:
    if annotation.get("paper_id") != paper_id or annotation.get("trashed_at") is not None:
        raise _fail("annotation_source_mismatch")
    if annotation.get("article_fingerprint") != article_fingerprint:
        raise _fail("annotation_source_mismatch")
    annotation_id = _uuid(annotation.get("id"), field="annotation_id")
    anchor = _json_object(annotation.get("anchor"), field="annotation_anchor")
    if anchor.get("selector_version") != SELECTOR_VERSION:
        raise _fail("annotation_selector_mismatch")
    node_id = anchor.get("node_id")
    if not isinstance(node_id, str) or node_id not in source_nodes:
        raise _fail("annotation_node_missing")
    exact_quote = anchor.get("exact_quote")
    selected = exact_quote.strip() if isinstance(exact_quote, str) else ""
    if selected and selected not in source_nodes[node_id]:
        raise _fail("annotation_exact_quote_mismatch")
    body_value = annotation.get("body")
    body = body_value.strip() if isinstance(body_value, str) else ""
    text = "\n".join(item for item in (selected, body) if item)
    if not text:
        return None
    share = annotation.get("ai_share_allowed", False)
    if type(share) is not bool:
        raise _fail("annotation_ai_share_invalid")
    return (
        {
            "schema_version": SEARCH_SCHEMA_VERSION,
            "selector_version": SELECTOR_VERSION,
            "source_kind": SourceKind.USER_ANNOTATION.value,
            "source_id": f"annotation:{annotation_id}",
            "node_id": node_id,
            "text": text,
        },
        share,
        annotation_id,
    )


def prepare_search_corpus(
    paper: Mapping[str, Any],
    article: dict[str, Any],
    *,
    reviewed_knowledge: Mapping[str, Any] | None = None,
    annotations: Iterable[Mapping[str, Any]] = (),
) -> PreparedSearchCorpus:
    """Build one deterministic corpus from current, authoritative records only."""

    paper_id = _positive_int(paper.get("id"), field="paper_id")
    source_sha256 = _sha256(paper.get("sha256"), field="source_sha256")
    selector_index = build_selector_index(article)
    source_nodes = article_source_nodes(article, selector_index)
    records: list[tuple[dict[str, Any], bool, str | None, str | None]] = []

    for node in article_source_node_records(article, selector_index):
        records.append(
            (
                {
                    "schema_version": SEARCH_SCHEMA_VERSION,
                    "selector_version": SELECTOR_VERSION,
                    "source_kind": SourceKind.SOURCE_TEXT.value,
                    "source_id": f"paper:{paper_id}",
                    "node_id": node["node_id"],
                    "text": node["text"],
                },
                True,
                None,
                None,
            )
        )

    if reviewed_knowledge is not None:
        try:
            canonical = validate_knowledge_payload(
                dict(reviewed_knowledge), source_nodes=source_nodes
            )
        except KnowledgeValidationError as exc:
            raise _fail("reviewed_knowledge_invalid") from exc
        if canonical["status"] != "reviewed":
            raise _fail("reviewed_knowledge_required")
        if (
            canonical["paper_identity"]["paper_id"] != paper_id
            or canonical["source_sha256"] != source_sha256
            or canonical["article_fingerprint"] != selector_index.article_fingerprint
            or canonical["selector_version"] != SELECTOR_VERSION
        ):
            raise _fail("reviewed_knowledge_source_mismatch")
        revision_id = _uuid(canonical["revision_id"], field="reviewed_revision_id")
        for claim_index, claim in enumerate(canonical["claims"]):
            for evidence_index, evidence in enumerate(claim["evidence"]):
                records.append(
                    (
                        {
                            "schema_version": SEARCH_SCHEMA_VERSION,
                            "selector_version": SELECTOR_VERSION,
                            "source_kind": SourceKind.REVIEWED_KNOWLEDGE.value,
                            "source_id": (
                                f"knowledge:{revision_id}:claim:{claim_index}:"
                                f"evidence:{evidence_index}"
                            ),
                            "node_id": evidence["node_id"],
                            "text": claim["text"],
                        },
                        True,
                        revision_id,
                        None,
                    )
                )

    try:
        annotation_values = list(annotations)
    except (TypeError, RuntimeError) as exc:
        raise _fail("annotations_invalid") from exc
    for annotation in annotation_values:
        if not isinstance(annotation, Mapping):
            raise _fail("annotation_invalid")
        prepared = _annotation_document(
            annotation,
            paper_id=paper_id,
            article_fingerprint=selector_index.article_fingerprint,
            source_nodes=source_nodes,
        )
        if prepared is not None:
            record, share, annotation_id = prepared
            records.append((record, share, None, annotation_id))

    try:
        normalized = normalize_search_documents(item[0] for item in records)
    except Exception as exc:
        if isinstance(exc, SearchStoreError):
            raise
        raise _fail("search_corpus_invalid") from exc
    metadata = {
        (
            item[0]["source_kind"],
            item[0]["source_id"],
            item[0]["node_id"],
        ): item[1:]
        for item in records
    }
    prepared_documents: list[PreparedSearchDocument] = []
    for document in normalized:
        model_eligible, revision_id, annotation_id = metadata[
            (document.source_kind.value, document.source_id, document.node_id)
        ]
        prepared_documents.append(
            PreparedSearchDocument(
                document=document,
                model_eligible=bool(model_eligible),
                reviewed_revision_id=revision_id,
                annotation_id=annotation_id,
            )
        )
    chunks = build_search_chunks(item[0] for item in records)
    corpus_payload = {
        "contract": "paper-search-corpus-v1",
        "paper_id": paper_id,
        "source_sha256": source_sha256,
        "article_fingerprint": selector_index.article_fingerprint,
        "selector_version": SELECTOR_VERSION,
        "documents": [
            {
                **item.document.to_dict(),
                "model_eligible": item.model_eligible,
                "reviewed_revision_id": item.reviewed_revision_id,
                "annotation_id": item.annotation_id,
            }
            for item in prepared_documents
        ],
        "chunks": [item.to_fts_row() | {"start": item.start, "end": item.end}
                   for item in chunks],
    }
    corpus_digest = _digest(corpus_payload)
    generation_id = str(
        uuid5(_GENERATION_NAMESPACE, f"{paper_id}:{corpus_digest}")
    )
    return PreparedSearchCorpus(
        paper_id=paper_id,
        generation_id=generation_id,
        corpus_digest=corpus_digest,
        source_sha256=source_sha256,
        article_fingerprint=selector_index.article_fingerprint,
        selector_version=SELECTOR_VERSION,
        documents=tuple(prepared_documents),
        chunks=chunks,
    )


def _current_paper(database: Any, paper_id: int) -> dict[str, Any]:
    row = database.execute(
        """SELECT id, sha256, reader_v2_status, reader_v2_manifest_path
           FROM papers WHERE id = %s FOR UPDATE""",
        (paper_id,),
    ).fetchone()
    paper = _row(row, code="paper_not_found")
    if (
        paper.get("reader_v2_status") != "ready"
        or paper.get("reader_v2_manifest_path") != f"{paper_id}/v2/article.json"
    ):
        raise SearchIndexNotReady("reader_v2_not_ready")
    _sha256(paper.get("sha256"), field="source_sha256")
    return paper


def _current_reviewed_knowledge(
    database: Any,
    *,
    paper_id: int,
    source_sha256: str,
    article_fingerprint: str,
) -> dict[str, Any] | None:
    row = database.execute(
        """SELECT r.canonical_payload
           FROM paper_knowledge_revisions r
           WHERE r.paper_id = %s
             AND r.status = 'reviewed'
             AND r.source_sha256 = %s
             AND r.article_fingerprint = %s
             AND r.selector_version = %s
             AND EXISTS (
                 SELECT 1 FROM paper_knowledge_review_events e
                 WHERE e.paper_id = r.paper_id
                   AND e.result_revision_id = r.id
                   AND e.decision = 'approve'
             )
           ORDER BY r.revision_number DESC, r.id DESC
           LIMIT 1""",
        (paper_id, source_sha256, article_fingerprint, SELECTOR_VERSION),
    ).fetchone()
    if row is None:
        return None
    return _json_object(dict(row).get("canonical_payload"), field="reviewed_payload")


def rebuild_paper_search_index(paper_id: int, data_dir: Path) -> dict[str, Any]:
    """Atomically move one paper's search head to a fully built generation."""

    clean_paper_id = _positive_int(paper_id, field="paper_id")
    if not isinstance(data_dir, Path):
        raise _fail("data_dir_invalid")
    with connection() as database:
        database.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"paper-search:{clean_paper_id}",),
        )
        paper = _current_paper(database, clean_paper_id)
        try:
            article = read_article(data_dir, clean_paper_id)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise SearchIndexNotReady("reader_v2_invalid") from exc
        index = build_selector_index(article)
        reviewed = _current_reviewed_knowledge(
            database,
            paper_id=clean_paper_id,
            source_sha256=str(paper["sha256"]),
            article_fingerprint=index.article_fingerprint,
        )
        annotation_rows = database.execute(
            """SELECT id, paper_id, kind, anchor, body, article_fingerprint,
                      trashed_at, ai_share_allowed
               FROM annotations
               WHERE paper_id = %s AND trashed_at IS NULL
               ORDER BY id""",
            (clean_paper_id,),
        ).fetchall()
        corpus = prepare_search_corpus(
            paper,
            article,
            reviewed_knowledge=reviewed,
            annotations=(dict(item) for item in annotation_rows),
        )
        inserted = database.execute(
            """INSERT INTO paper_search_generations
               (id, paper_id, corpus_digest, source_sha256,
                article_fingerprint, selector_version, document_count,
                chunk_count)
               VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (paper_id, corpus_digest) DO NOTHING
               RETURNING id""",
            (
                corpus.generation_id,
                corpus.paper_id,
                corpus.corpus_digest,
                corpus.source_sha256,
                corpus.article_fingerprint,
                corpus.selector_version,
                len(corpus.documents),
                len(corpus.chunks),
            ),
        ).fetchone()
        if inserted is not None:
            for prepared in corpus.documents:
                document = prepared.document
                database.execute(
                    """INSERT INTO paper_search_documents
                       (generation_id, paper_id, document_id, selector_version,
                        source_kind, source_id, reviewed_revision_id,
                        annotation_id, node_id, content, lexical_content,
                        authority_score, model_eligible, content_sha256)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::uuid,
                               %s::uuid, %s, %s, %s, %s, %s, %s)""",
                    (
                        corpus.generation_id,
                        corpus.paper_id,
                        document.document_id,
                        document.selector_version,
                        document.source_kind.value,
                        document.source_id,
                        prepared.reviewed_revision_id,
                        prepared.annotation_id,
                        document.node_id,
                        document.text,
                        document.lexical_text,
                        document.authority_score,
                        prepared.model_eligible,
                        hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
                    ),
                )
            eligibility = corpus.model_eligibility
            origins = corpus.document_origins
            for chunk in corpus.chunks:
                reviewed_revision_id, annotation_id = origins[chunk.document_id]
                database.execute(
                    """INSERT INTO paper_search_chunks
                       (generation_id, paper_id, chunk_id, document_id,
                        selector_version, source_kind, source_id,
                        reviewed_revision_id, annotation_id, node_id,
                        start_offset, end_offset, content, lexical_content,
                        authority_score, model_eligible, content_sha256)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s::uuid,
                               %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        corpus.generation_id,
                        corpus.paper_id,
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.selector_version,
                        chunk.source_kind.value,
                        chunk.source_id,
                        reviewed_revision_id,
                        annotation_id,
                        chunk.node_id,
                        chunk.start,
                        chunk.end,
                        chunk.text,
                        chunk.lexical_text,
                        chunk.authority_score,
                        eligibility[chunk.document_id],
                        hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                    ),
                )
        else:
            existing = database.execute(
                """SELECT id, source_sha256, article_fingerprint,
                          selector_version, document_count, chunk_count
                   FROM paper_search_generations
                   WHERE paper_id = %s AND corpus_digest = %s""",
                (corpus.paper_id, corpus.corpus_digest),
            ).fetchone()
            stored = _row(existing, code="search_generation_missing")
            expected = (
                corpus.generation_id,
                corpus.source_sha256,
                corpus.article_fingerprint,
                corpus.selector_version,
                len(corpus.documents),
                len(corpus.chunks),
            )
            actual = tuple(
                str(stored.get(key)) if key in {"id", "source_sha256", "article_fingerprint"}
                else stored.get(key)
                for key in (
                    "id", "source_sha256", "article_fingerprint",
                    "selector_version", "document_count", "chunk_count",
                )
            )
            if actual != expected:
                raise _fail("search_generation_conflict")
        database.execute(
            """INSERT INTO paper_search_heads
               (paper_id, generation_id, corpus_digest, source_sha256,
                article_fingerprint, selector_version)
               VALUES (%s, %s::uuid, %s, %s, %s, %s)
               ON CONFLICT (paper_id) DO UPDATE SET
                   generation_id = EXCLUDED.generation_id,
                   corpus_digest = EXCLUDED.corpus_digest,
                   source_sha256 = EXCLUDED.source_sha256,
                   article_fingerprint = EXCLUDED.article_fingerprint,
                   selector_version = EXCLUDED.selector_version,
                   updated_at = NOW()""",
            (
                corpus.paper_id,
                corpus.generation_id,
                corpus.corpus_digest,
                corpus.source_sha256,
                corpus.article_fingerprint,
                corpus.selector_version,
            ),
        )
        return {
            "paper_id": corpus.paper_id,
            "generation_id": corpus.generation_id,
            "corpus_digest": corpus.corpus_digest,
            "document_count": len(corpus.documents),
            "chunk_count": len(corpus.chunks),
            "reused_generation": inserted is None,
        }


def _row_to_chunk(row: Mapping[str, Any]) -> SearchChunk:
    try:
        source_kind = SourceKind(str(row["source_kind"]))
        return SearchChunk(
            schema_version=SEARCH_SCHEMA_VERSION,
            selector_version=int(row["selector_version"]),
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            source_kind=source_kind,
            source_id=str(row["source_id"]),
            node_id=str(row["node_id"]),
            start=int(row["start_offset"]),
            end=int(row["end_offset"]),
            text=str(row["content"]),
            lexical_text=str(row["lexical_content"]),
            authority_score=int(row["authority_score"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("stored_search_chunk_invalid") from exc


def _validated_search_input(query: Any, limit: Any) -> tuple[str, tuple[str, ...], int]:
    try:
        query_text = canonical_plain_text(
            query, field="query", limit=MAX_QUERY_CHARS
        )
        terms = lexical_terms(query_text)
    except Exception as exc:
        raise _fail("search_query_invalid") from exc
    clean_limit = _positive_int(limit, field="search_limit", maximum=MAX_SEARCH_LIMIT)
    return query_text, terms, clean_limit


def _search_database(
    database: Any,
    query: Any,
    *,
    paper_id: int | None,
    limit: int,
    include_personal_annotations: bool,
    model_eligible_only: bool,
) -> tuple[SearchHit, ...]:
    query_text, terms, clean_limit = _validated_search_input(query, limit)
    if type(include_personal_annotations) is not bool:
        raise _fail("include_personal_annotations_invalid")
    if type(model_eligible_only) is not bool:
        raise _fail("model_eligible_only_invalid")
    conditions = [
        "EXISTS (SELECT 1 FROM unnest(%s::text[]) AS q(term) "
        "WHERE position(q.term IN c.lexical_content) > 0 "
        "OR c.search_vector @@ plainto_tsquery('simple'::regconfig, q.term))"
    ]
    parameters: list[Any] = [list(terms)]
    if paper_id is not None:
        conditions.append("h.paper_id = %s")
        parameters.append(_positive_int(paper_id, field="paper_id"))
    if not include_personal_annotations:
        conditions.append("c.source_kind <> 'user_annotation'")
    if model_eligible_only:
        conditions.append("c.model_eligible")
    candidate_limit = min(
        _MAX_DATABASE_CANDIDATES, max(500, clean_limit * 50)
    )
    parameters.extend([query_text, candidate_limit])
    rows = database.execute(
        f"""SELECT c.paper_id, c.chunk_id, c.document_id, c.selector_version,
                   c.source_kind, c.source_id, c.node_id, c.start_offset,
                   c.end_offset, c.content, c.lexical_content,
                   c.authority_score
            FROM paper_search_heads h
            JOIN paper_search_chunks c
              ON c.generation_id = h.generation_id
             AND c.paper_id = h.paper_id
            WHERE {' AND '.join(conditions)}
            ORDER BY c.authority_score DESC,
                     ts_rank_cd(
                         c.search_vector,
                         plainto_tsquery('simple'::regconfig, %s)
                     ) DESC,
                     c.source_kind, c.source_id, c.node_id, c.chunk_id
            LIMIT %s""",
        tuple(parameters),
    ).fetchall()
    row_values = tuple(dict(row) for row in rows)
    chunks = tuple(_row_to_chunk(row) for row in row_values)
    try:
        hits = search_chunks(query_text, chunks, limit=clean_limit)
    except Exception as exc:
        raise _fail("stored_search_chunk_invalid") from exc
    paper_ids: dict[str, int] = {}
    for row in row_values:
        chunk_id = str(row.get("chunk_id"))
        value = _positive_int(row.get("paper_id"), field="stored_paper_id")
        if chunk_id in paper_ids and paper_ids[chunk_id] != value:
            raise _fail("stored_search_chunk_identity_conflict")
        paper_ids[chunk_id] = value
    return tuple(
        SearchHit(
            chunk=hit.chunk,
            lexical_score=hit.lexical_score,
            authority_score=hit.authority_score,
            total_score=hit.total_score,
            paper_id=paper_ids[hit.chunk.chunk_id],
        )
        for hit in hits
    )


def search_papers(
    query: Any,
    *,
    paper_id: int | None = None,
    limit: int = 20,
    include_personal_annotations: bool = True,
) -> tuple[SearchHit, ...]:
    """Search only current generations; authority is a hard ranking boundary."""

    _validated_search_input(query, limit)
    if type(include_personal_annotations) is not bool:
        raise _fail("include_personal_annotations_invalid")
    with connection() as database:
        return _search_database(
            database,
            query,
            paper_id=paper_id,
            limit=limit,
            include_personal_annotations=include_personal_annotations,
            model_eligible_only=False,
        )


def qa_job_identity(paper_id: int, idempotency_key: str) -> tuple[str, str]:
    clean_paper_id = _positive_int(paper_id, field="paper_id")
    clean_key = _bounded_text(
        idempotency_key, field="idempotency_key", maximum=200
    )
    key_digest = hashlib.sha256(clean_key.encode("utf-8")).hexdigest()
    return str(uuid5(_QA_JOB_NAMESPACE, f"{clean_paper_id}:{key_digest}")), key_digest


def _human_qa_request_authorization(
    actor_kind: Any,
    actor_id: Any,
    request_acknowledged: Any,
) -> tuple[str, str, bool]:
    if actor_kind != "human":
        raise _fail("qa_request_human_actor_required")
    clean_actor = _bounded_text(actor_id, field="request_actor_id", maximum=200)
    if request_acknowledged is not True:
        raise _fail("qa_request_acknowledgement_required")
    return "human", clean_actor, True


def _job_human_qa_request_authorization(
    job: Mapping[str, Any],
) -> tuple[str, str, bool]:
    if not isinstance(job, Mapping):
        raise _fail("qa_job_authorization_invalid")
    return _human_qa_request_authorization(
        job.get("request_actor_kind"),
        job.get("request_actor_id"),
        job.get("request_acknowledged"),
    )


def _qa_request_digest(
    *,
    paper_id: int,
    provider: str,
    model: str,
    request_actor_kind: str,
    request_actor_id: str,
    request_acknowledged: bool,
    include_personal_annotations: bool,
    evidence_limit: int,
    evidence_snapshot_sha256: str,
) -> str:
    actor_kind, actor_id, acknowledged = _human_qa_request_authorization(
        request_actor_kind,
        request_actor_id,
        request_acknowledged,
    )
    return _digest(
        {
            "contract": "paper-qa-job-v2",
            "paper_id": paper_id,
            "provider": provider,
            "model": model,
            "request_actor_kind": actor_kind,
            "request_actor_id": actor_id,
            "request_acknowledged": acknowledged,
            "include_personal_annotations": include_personal_annotations,
            "evidence_limit": evidence_limit,
            "evidence_snapshot_sha256": evidence_snapshot_sha256,
        }
    )


def _validate_qa_job_request_authorization(
    job: Mapping[str, Any],
) -> tuple[str, str, bool]:
    """Validate durable human authorization and its exact QA request digest."""

    actor_kind, actor_id, acknowledged = _job_human_qa_request_authorization(job)
    if type(job.get("include_personal_annotations")) is not bool:
        raise _fail("stored_include_personal_annotations_invalid")
    expected = _qa_request_digest(
        paper_id=_positive_int(job.get("paper_id"), field="paper_id"),
        provider=_bounded_text(job.get("provider"), field="provider", maximum=80),
        model=_bounded_text(job.get("model"), field="model", maximum=200),
        request_actor_kind=actor_kind,
        request_actor_id=actor_id,
        request_acknowledged=acknowledged,
        include_personal_annotations=job["include_personal_annotations"],
        evidence_limit=_positive_int(
            job.get("evidence_limit"),
            field="evidence_limit",
            maximum=MAX_SEARCH_LIMIT,
        ),
        evidence_snapshot_sha256=_sha256(
            job.get("evidence_snapshot_sha256"),
            field="evidence_snapshot_sha256",
        ),
    )
    if _sha256(job.get("request_digest"), field="request_digest") != expected:
        raise _fail("qa_request_digest_mismatch")
    return actor_kind, actor_id, acknowledged


def enqueue_qa_job(
    paper_id: int,
    question: Any,
    *,
    idempotency_key: str,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    request_actor_kind: str,
    request_actor_id: str,
    request_acknowledged: bool,
    include_personal_annotations: bool = False,
    evidence_limit: int = 32,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Snapshot current eligible evidence and enqueue one asynchronous QA job."""

    clean_paper_id = _positive_int(paper_id, field="paper_id")
    clean_provider = _bounded_text(provider, field="provider", maximum=80)
    clean_model = _bounded_text(model, field="model", maximum=200)
    actor_kind, actor_id, acknowledged = _human_qa_request_authorization(
        request_actor_kind,
        request_actor_id,
        request_acknowledged,
    )
    if type(include_personal_annotations) is not bool:
        raise _fail("include_personal_annotations_invalid")
    clean_limit = _positive_int(
        evidence_limit, field="evidence_limit", maximum=MAX_SEARCH_LIMIT
    )
    question_text, _, _ = _validated_search_input(question, clean_limit)
    clean_attempts = _positive_int(max_attempts, field="max_attempts", maximum=10)
    job_id, key_digest = qa_job_identity(clean_paper_id, idempotency_key)

    with connection() as database:
        database.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"paper-qa-enqueue:{clean_paper_id}:{key_digest}",),
        )
        head_row = database.execute(
            """SELECT h.generation_id, h.source_sha256,
                      h.article_fingerprint, h.selector_version
               FROM paper_search_heads h
               WHERE h.paper_id = %s""",
            (clean_paper_id,),
        ).fetchone()
        if head_row is None:
            raise SearchIndexNotReady("search_index_not_ready")
        head = dict(head_row)
        hits = _search_database(
            database,
            question_text,
            paper_id=clean_paper_id,
            limit=clean_limit,
            include_personal_annotations=include_personal_annotations,
            model_eligible_only=True,
        )
        evidence_by_chunk = {hit.chunk.chunk_id: hit.chunk for hit in hits}
        snapshot_input = {
            "schema_version": 1,
            "selector_version": int(head["selector_version"]),
            "question": question_text,
            "source_nodes": [
                {
                    "selector_version": hit.chunk.selector_version,
                    "source_kind": hit.chunk.source_kind.value,
                    "source_id": hit.chunk.chunk_id,
                    "node_id": hit.chunk.node_id,
                    "text": hit.chunk.text,
                }
                for hit in hits
            ],
        }
        try:
            snapshot = validate_qa_request(snapshot_input)
        except QAContractError as exc:
            raise _fail("qa_evidence_snapshot_invalid") from exc
        snapshot_digest = _digest(snapshot)
        request_digest = _qa_request_digest(
            paper_id=clean_paper_id,
            provider=clean_provider,
            model=clean_model,
            request_actor_kind=actor_kind,
            request_actor_id=actor_id,
            request_acknowledged=acknowledged,
            include_personal_annotations=include_personal_annotations,
            evidence_limit=clean_limit,
            evidence_snapshot_sha256=snapshot_digest,
        )
        inserted = database.execute(
            f"""INSERT INTO paper_qa_jobs
                (id, paper_id, idempotency_key_digest, request_digest,
                 question, provider, model, request_actor_kind,
                 request_actor_id, request_acknowledged,
                 include_personal_annotations,
                 evidence_limit, generation_id, source_sha256,
                 article_fingerprint, selector_version, evidence_snapshot,
                 evidence_snapshot_sha256, max_attempts)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::uuid, %s, %s, %s,
                        %s::jsonb, %s, %s)
                ON CONFLICT (paper_id, idempotency_key_digest) DO NOTHING
                RETURNING {QA_JOB_COLUMNS}""",
            (
                job_id,
                clean_paper_id,
                key_digest,
                request_digest,
                snapshot["question"],
                clean_provider,
                clean_model,
                actor_kind,
                actor_id,
                acknowledged,
                include_personal_annotations,
                clean_limit,
                str(head["generation_id"]),
                str(head["source_sha256"]),
                str(head["article_fingerprint"]),
                int(head["selector_version"]),
                _json_text(snapshot),
                snapshot_digest,
                clean_attempts,
            ),
        ).fetchone()
        if inserted is None:
            existing = database.execute(
                f"""SELECT {QA_JOB_COLUMNS} FROM paper_qa_jobs
                    WHERE paper_id = %s AND idempotency_key_digest = %s""",
                (clean_paper_id, key_digest),
            ).fetchone()
            job = _row(existing, code="qa_job_missing")
            if (
                str(job.get("request_digest")) != request_digest
                or _validate_qa_job_request_authorization(job)
                != (actor_kind, actor_id, acknowledged)
            ):
                raise _fail("qa_idempotency_conflict")
            return job
        job = dict(inserted)
        for ordinal, node in enumerate(snapshot["source_nodes"]):
            chunk = evidence_by_chunk.get(node["source_id"])
            if chunk is None:
                raise _fail("qa_evidence_snapshot_mismatch")
            database.execute(
                """INSERT INTO paper_qa_job_evidence
                   (job_id, paper_id, ordinal, generation_id, chunk_id,
                    source_kind, node_id, content_sha256, model_eligible)
                   VALUES (%s::uuid, %s, %s, %s::uuid, %s, %s, %s, %s, TRUE)""",
                (
                    job_id,
                    clean_paper_id,
                    ordinal,
                    str(head["generation_id"]),
                    chunk.chunk_id,
                    chunk.source_kind.value,
                    chunk.node_id,
                    hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                ),
            )
        return job


def _recover_expired_qa_jobs(database: Any) -> None:
    database.execute(
        """UPDATE paper_qa_jobs
           SET status = CASE
                   WHEN attempt_count >= max_attempts THEN 'failed'
                   ELSE 'retry_wait'
               END,
               next_attempt_at = NOW(),
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at = NULL,
               last_error_code = 'lease_expired',
               finished_at = CASE
                   WHEN attempt_count >= max_attempts THEN NOW()
                   ELSE NULL
               END
           WHERE status = 'running' AND lease_expires_at <= NOW()"""
    )


def claim_qa_job(worker_id: str, *, lease_seconds: int = 300) -> dict[str, Any] | None:
    clean_worker = _bounded_text(worker_id, field="worker_id", maximum=200)
    clean_lease = _positive_int(
        lease_seconds, field="lease_seconds", maximum=3_600
    )
    lease_token = str(uuid4())
    with connection() as database:
        _recover_expired_qa_jobs(database)
        row = database.execute(
            f"""WITH candidate AS (
                    SELECT id FROM paper_qa_jobs
                    WHERE status IN ('queued', 'retry_wait')
                      AND next_attempt_at <= NOW()
                      AND attempt_count < max_attempts
                      AND request_actor_kind = 'human'
                      AND request_acknowledged IS TRUE
                    ORDER BY next_attempt_at, requested_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE paper_qa_jobs j
                SET status = 'running',
                    attempt_count = j.attempt_count + 1,
                    lease_owner = %s,
                    lease_token = %s::uuid,
                    lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    started_at = COALESCE(j.started_at, NOW()),
                    last_error_code = NULL
                FROM candidate
                WHERE j.id = candidate.id
                RETURNING {QA_JOB_COLUMNS}""",
            (clean_worker, lease_token, clean_lease),
        ).fetchone()
        if row is None:
            return None
        claimed = dict(row)
        _validate_qa_job_request_authorization(claimed)
        return claimed


def _canonical_snapshot(job: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _json_object(job.get("evidence_snapshot"), field="evidence_snapshot")
    try:
        canonical = validate_qa_request(snapshot)
    except QAContractError as exc:
        raise _fail("stored_evidence_snapshot_invalid") from exc
    stored_digest = _sha256(
        job.get("evidence_snapshot_sha256"), field="evidence_snapshot_sha256"
    )
    if _digest(canonical) != stored_digest:
        raise _fail("stored_evidence_snapshot_digest_mismatch")
    if (
        canonical["question"] != job.get("question")
        or canonical["selector_version"] != job.get("selector_version")
    ):
        raise _fail("stored_evidence_snapshot_mismatch")
    return canonical


def validate_qa_evidence_snapshot(
    job: Mapping[str, Any],
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    current_source_sha256: str,
    current_article_fingerprint: str,
    current_selector_version: int,
) -> dict[str, Any]:
    """Revalidate a stored model boundary without trusting cached source labels."""

    canonical = _canonical_snapshot(job)
    if (
        _sha256(current_source_sha256, field="current_source_sha256")
        != job.get("source_sha256")
        or _sha256(
            current_article_fingerprint, field="current_article_fingerprint"
        )
        != job.get("article_fingerprint")
        or current_selector_version != SELECTOR_VERSION
        or job.get("selector_version") != SELECTOR_VERSION
    ):
        raise _fail("qa_evidence_stale")
    if type(job.get("include_personal_annotations")) is not bool:
        raise _fail("stored_include_personal_annotations_invalid")
    if len(evidence_rows) != len(canonical["source_nodes"]):
        raise _fail("qa_evidence_rows_mismatch")
    by_ordinal: dict[int, Mapping[str, Any]] = {}
    for value in evidence_rows:
        try:
            ordinal = int(value["ordinal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail("qa_evidence_row_invalid") from exc
        if ordinal in by_ordinal or not 0 <= ordinal < len(evidence_rows):
            raise _fail("qa_evidence_row_invalid")
        by_ordinal[ordinal] = value
    for ordinal, node in enumerate(canonical["source_nodes"]):
        row = by_ordinal.get(ordinal)
        if row is None:
            raise _fail("qa_evidence_row_invalid")
        content = row.get("content")
        if not isinstance(content, str):
            raise _fail("qa_evidence_row_invalid")
        if (
            node["source_id"] != str(row.get("chunk_id"))
            or node["source_kind"] != str(row.get("source_kind"))
            or node["node_id"] != str(row.get("node_id"))
            or node["text"] != content
            or hashlib.sha256(content.encode("utf-8")).hexdigest()
            != str(row.get("content_sha256"))
            or row.get("model_eligible") is not True
        ):
            raise _fail("qa_evidence_row_mismatch")
        kind = SourceKind(node["source_kind"])
        if kind == SourceKind.USER_ANNOTATION:
            if not job["include_personal_annotations"]:
                raise _fail("qa_annotation_not_opted_in")
            if (
                row.get("annotation_active") is not True
                or row.get("annotation_ai_share_allowed") is not True
                or row.get("annotation_article_fingerprint")
                != current_article_fingerprint
                or row.get("annotation_node_id") != node["node_id"]
            ):
                raise _fail("qa_annotation_not_shareable")
            anchor = _json_object(
                row.get("annotation_anchor"), field="annotation_anchor"
            )
            if (
                anchor.get("selector_version") != SELECTOR_VERSION
                or anchor.get("node_id") != node["node_id"]
            ):
                raise _fail("qa_annotation_not_shareable")
            selected_value = anchor.get("exact_quote")
            selected = (
                selected_value.strip() if isinstance(selected_value, str) else ""
            )
            body_value = row.get("annotation_body")
            body = body_value.strip() if isinstance(body_value, str) else ""
            current_annotation_text = "\n".join(
                item for item in (selected, body) if item
            )
            try:
                start = int(row.get("start_offset"))
                end = int(row.get("end_offset"))
            except (TypeError, ValueError) as exc:
                raise _fail("qa_annotation_not_shareable") from exc
            if (
                not current_annotation_text
                or not 0 <= start < end <= len(current_annotation_text)
                or current_annotation_text[start:end] != content
            ):
                raise _fail("qa_annotation_changed")
        elif kind == SourceKind.REVIEWED_KNOWLEDGE:
            if (
                row.get("reviewed_status") != "reviewed"
                or row.get("reviewed_approved") is not True
                or row.get("reviewed_is_current") is not True
                or row.get("reviewed_source_sha256") != current_source_sha256
                or row.get("reviewed_article_fingerprint")
                != current_article_fingerprint
                or row.get("reviewed_selector_version") != SELECTOR_VERSION
            ):
                raise _fail("qa_reviewed_knowledge_stale")
        elif kind != SourceKind.SOURCE_TEXT:
            raise _fail("qa_source_kind_forbidden")
    return canonical


def _assert_live_lease(job: Mapping[str, Any], lease_token: str) -> None:
    if (
        job.get("status") != "running"
        or str(job.get("lease_token")) != lease_token
        or job.get("lease_is_current") is not True
    ):
        raise QALeaseLost("qa_lease_lost")


def load_qa_model_request(
    job_id: str | UUID,
    lease_token: str | UUID,
    *,
    data_dir: Path,
) -> dict[str, Any]:
    """Return a prompt only after rechecking live provenance and sharing consent."""

    clean_job_id = _uuid(job_id, field="job_id")
    clean_token = _uuid(lease_token, field="lease_token")
    if not isinstance(data_dir, Path):
        raise _fail("data_dir_invalid")
    with connection() as database:
        row = database.execute(
            f"""SELECT {', '.join('j.' + item.strip() for item in QA_JOB_COLUMNS.split(','))},
                       (j.lease_expires_at > NOW()) AS lease_is_current,
                       h.generation_id AS current_generation_id,
                       h.source_sha256 AS current_source_sha256,
                       h.article_fingerprint AS current_article_fingerprint,
                       h.selector_version AS current_selector_version,
                       p.reader_v2_status, p.reader_v2_manifest_path,
                       p.sha256 AS paper_source_sha256
                FROM paper_qa_jobs j
                JOIN papers p ON p.id = j.paper_id
                JOIN paper_search_heads h ON h.paper_id = j.paper_id
                WHERE j.id = %s::uuid
                FOR UPDATE OF j, p""",
            (clean_job_id,),
        ).fetchone()
        job = _row(row, code="qa_job_missing")
        _validate_qa_job_request_authorization(job)
        _assert_live_lease(job, clean_token)
        paper_id = _positive_int(job.get("paper_id"), field="paper_id")
        if (
            str(job.get("generation_id")) != str(job.get("current_generation_id"))
            or job.get("reader_v2_status") != "ready"
            or job.get("reader_v2_manifest_path") != f"{paper_id}/v2/article.json"
            or str(job.get("paper_source_sha256"))
            != str(job.get("current_source_sha256"))
        ):
            raise _fail("qa_evidence_stale")
        try:
            article = read_article(data_dir, paper_id)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise _fail("qa_current_article_invalid") from exc
        selector_index = build_selector_index(article)
        evidence_rows = database.execute(
            """SELECT e.ordinal, e.chunk_id, e.source_kind, e.node_id,
                      e.content_sha256, e.model_eligible, c.content,
                      c.start_offset, c.end_offset,
                      r.status AS reviewed_status,
                      r.source_sha256 AS reviewed_source_sha256,
                      r.article_fingerprint AS reviewed_article_fingerprint,
                      r.selector_version AS reviewed_selector_version,
                      EXISTS (
                          SELECT 1 FROM paper_knowledge_review_events re
                          WHERE re.paper_id = e.paper_id
                            AND re.result_revision_id = r.id
                            AND re.decision = 'approve'
                      ) AS reviewed_approved,
                      (r.id = (
                          SELECT current_revision.id
                          FROM paper_knowledge_revisions current_revision
                          WHERE current_revision.paper_id = e.paper_id
                            AND current_revision.status = 'reviewed'
                            AND current_revision.source_sha256 = %s
                            AND current_revision.article_fingerprint = %s
                            AND current_revision.selector_version = %s
                            AND EXISTS (
                                SELECT 1
                                FROM paper_knowledge_review_events current_event
                                WHERE current_event.paper_id = e.paper_id
                                  AND current_event.result_revision_id = current_revision.id
                                  AND current_event.decision = 'approve'
                            )
                          ORDER BY current_revision.revision_number DESC,
                                   current_revision.id DESC
                          LIMIT 1
                      )) AS reviewed_is_current,
                      (a.id IS NOT NULL AND a.trashed_at IS NULL) AS annotation_active,
                      a.ai_share_allowed AS annotation_ai_share_allowed,
                      a.article_fingerprint AS annotation_article_fingerprint,
                      a.anchor ->> 'node_id' AS annotation_node_id,
                      a.anchor AS annotation_anchor,
                      a.body AS annotation_body
               FROM paper_qa_job_evidence e
               JOIN paper_search_chunks c
                 ON c.generation_id = e.generation_id
                AND c.paper_id = e.paper_id
                AND c.chunk_id = e.chunk_id
               LEFT JOIN paper_knowledge_revisions r
                 ON r.id = c.reviewed_revision_id AND r.paper_id = c.paper_id
               LEFT JOIN annotations a
                 ON a.id = c.annotation_id AND a.paper_id = c.paper_id
               WHERE e.job_id = %s::uuid
               ORDER BY e.ordinal""",
            (
                str(job["current_source_sha256"]),
                selector_index.article_fingerprint,
                selector_index.selector_version,
                clean_job_id,
            ),
        ).fetchall()
        canonical = validate_qa_evidence_snapshot(
            job,
            [dict(item) for item in evidence_rows],
            current_source_sha256=str(job["current_source_sha256"]),
            current_article_fingerprint=selector_index.article_fingerprint,
            current_selector_version=selector_index.selector_version,
        )
        if selector_index.article_fingerprint != job.get("current_article_fingerprint"):
            raise _fail("qa_evidence_stale")
        try:
            prompt = build_qa_prompt_payload(
                canonical,
                include_user_annotations=job["include_personal_annotations"],
            )
        except QAContractError as exc:
            raise _fail("qa_prompt_invalid") from exc
        return {
            "job_id": clean_job_id,
            "attempt_number": int(job["attempt_count"]),
            "provider": str(job["provider"]),
            "model": str(job["model"]),
            "request": canonical,
            "prompt": prompt,
            "request_digest": str(job["request_digest"]),
        }


def _qa_usage_id(job_id: str, attempt_number: int) -> str:
    return str(uuid5(_QA_USAGE_NAMESPACE, f"{job_id}:{attempt_number}"))


def _record_qa_usage(
    database: Any,
    job: Mapping[str, Any],
    *,
    response_digest: str,
    usage: QAUsage,
) -> dict[str, Any]:
    if not isinstance(usage, QAUsage):
        raise _fail("qa_usage_invalid")
    if usage.provider != job.get("provider") or usage.model != job.get("model"):
        raise _fail("qa_usage_provider_model_mismatch")
    clean_response_digest = _sha256(
        response_digest, field="response_digest"
    )
    job_id = _uuid(job.get("id"), field="job_id")
    attempt_number = _positive_int(
        job.get("attempt_count"), field="attempt_number"
    )
    usage_id = _qa_usage_id(job_id, attempt_number)
    inserted = database.execute(
        """INSERT INTO paper_qa_usage_ledger
           (id, job_id, attempt_number, provider, model, request_digest,
            response_digest, prompt_tokens, completion_tokens, total_tokens,
            cost_microusd, cost_basis, pricing_version)
           VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s)
           ON CONFLICT (job_id, attempt_number) DO NOTHING
           RETURNING id, job_id, attempt_number, provider, model,
                     request_digest, response_digest, prompt_tokens,
                     completion_tokens, total_tokens, cost_microusd,
                     cost_basis, pricing_version, recorded_at""",
        (
            usage_id,
            job_id,
            attempt_number,
            usage.provider,
            usage.model,
            _sha256(job.get("request_digest"), field="request_digest"),
            clean_response_digest,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cost_microusd,
            usage.cost_basis,
            usage.pricing_version,
        ),
    ).fetchone()
    if inserted is not None:
        return dict(inserted)
    existing = database.execute(
        """SELECT id, job_id, attempt_number, provider, model,
                  request_digest, response_digest, prompt_tokens,
                  completion_tokens, total_tokens, cost_microusd,
                  cost_basis, pricing_version, recorded_at
           FROM paper_qa_usage_ledger
           WHERE job_id = %s::uuid AND attempt_number = %s""",
        (job_id, attempt_number),
    ).fetchone()
    row = _row(existing, code="qa_usage_missing")
    comparisons = {
        "provider": usage.provider,
        "model": usage.model,
        "request_digest": str(job["request_digest"]),
        "response_digest": clean_response_digest,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cost_microusd": usage.cost_microusd,
        "cost_basis": usage.cost_basis,
        "pricing_version": usage.pricing_version,
    }
    if any(row.get(key) != value for key, value in comparisons.items()):
        raise _fail("qa_usage_conflict")
    return row


def _locked_qa_job(database: Any, job_id: str) -> dict[str, Any]:
    row = database.execute(
        f"""SELECT {QA_JOB_COLUMNS},
                   (lease_expires_at > NOW()) AS lease_is_current
            FROM paper_qa_jobs
            WHERE id = %s::uuid
            FOR UPDATE""",
        (job_id,),
    ).fetchone()
    return _row(row, code="qa_job_missing")


def record_qa_usage(
    job_id: str | UUID,
    lease_token: str | UUID,
    *,
    response_digest: str,
    usage: QAUsage,
) -> dict[str, Any]:
    clean_job_id = _uuid(job_id, field="job_id")
    clean_token = _uuid(lease_token, field="lease_token")
    with connection() as database:
        job = _locked_qa_job(database, clean_job_id)
        _assert_live_lease(job, clean_token)
        return _record_qa_usage(
            database, job, response_digest=response_digest, usage=usage
        )


def complete_qa_job(
    job_id: str | UUID,
    lease_token: str | UUID,
    *,
    response: Any,
    usage: QAUsage,
) -> dict[str, Any]:
    """Validate every citation, append usage, then store the canonical answer."""

    clean_job_id = _uuid(job_id, field="job_id")
    clean_token = _uuid(lease_token, field="lease_token")
    with connection() as database:
        job = _locked_qa_job(database, clean_job_id)
        _assert_live_lease(job, clean_token)
        snapshot = _canonical_snapshot(job)
        try:
            answer = validate_qa_response(response, request=snapshot)
        except QAContractError as exc:
            raise _fail("qa_response_invalid") from exc
        response_digest = _digest(answer)
        _record_qa_usage(
            database, job, response_digest=response_digest, usage=usage
        )
        updated = database.execute(
            f"""UPDATE paper_qa_jobs
                SET status = 'succeeded', answer_payload = %s::jsonb,
                    response_digest = %s, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    last_error_code = NULL, finished_at = NOW()
                WHERE id = %s::uuid AND status = 'running'
                  AND lease_token = %s::uuid AND lease_expires_at > NOW()
                RETURNING {QA_JOB_COLUMNS}""",
            (_json_text(answer), response_digest, clean_job_id, clean_token),
        ).fetchone()
        if updated is None:
            raise QALeaseLost("qa_lease_lost")
        return dict(updated)


def nack_qa_job(
    job_id: str | UUID,
    lease_token: str | UUID,
    *,
    error_code: str,
    retry_delay_seconds: int = 60,
) -> dict[str, Any]:
    """Release a lease with a sanitized code, retrying only within the attempt cap."""

    clean_job_id = _uuid(job_id, field="job_id")
    clean_token = _uuid(lease_token, field="lease_token")
    if not isinstance(error_code, str) or not _ERROR_CODE_RE.fullmatch(error_code):
        raise _fail("qa_error_code_invalid")
    delay = _positive_int(
        retry_delay_seconds, field="retry_delay_seconds", maximum=86_400
    )
    with connection() as database:
        job = _locked_qa_job(database, clean_job_id)
        _assert_live_lease(job, clean_token)
        terminal = int(job["attempt_count"]) >= int(job["max_attempts"])
        updated = database.execute(
            f"""UPDATE paper_qa_jobs
                SET status = %s,
                    next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = %s,
                    finished_at = CASE WHEN %s THEN NOW() ELSE NULL END
                WHERE id = %s::uuid AND status = 'running'
                  AND lease_token = %s::uuid AND lease_expires_at > NOW()
                RETURNING {QA_JOB_COLUMNS}""",
            (
                "failed" if terminal else "retry_wait",
                delay,
                error_code,
                terminal,
                clean_job_id,
                clean_token,
            ),
        ).fetchone()
        if updated is None:
            raise QALeaseLost("qa_lease_lost")
        return dict(updated)


def get_qa_job(job_id: str | UUID) -> dict[str, Any]:
    clean_job_id = _uuid(job_id, field="job_id")
    with connection() as database:
        row = database.execute(
            f"SELECT {QA_JOB_COLUMNS} FROM paper_qa_jobs WHERE id = %s::uuid",
            (clean_job_id,),
        ).fetchone()
        job = _row(row, code="qa_job_missing")
        snapshot = _canonical_snapshot(job)
        if job.get("answer_payload") is not None:
            answer = _json_object(job["answer_payload"], field="answer_payload")
            try:
                job["answer_payload"] = validate_qa_response(
                    answer, request=snapshot
                )
            except QAContractError as exc:
                raise _fail("stored_qa_response_invalid") from exc
            if _digest(job["answer_payload"]) != _sha256(
                job.get("response_digest"), field="response_digest"
            ):
                raise _fail("stored_qa_response_digest_mismatch")
        return job
