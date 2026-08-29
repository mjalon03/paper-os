"""Local PDF text extraction and validated DeepSeek metadata classification."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATEGORIES = (
    "Neuroscience",
    "NeuroAI",
    "Machine Learning",
    "Computational Biology",
    "Methods",
    "Other",
)
PAPER_TYPES = ("Research", "Review", "Methods", "Benchmark", "Perspective", "Other")
DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


class MetadataFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExtractedContext:
    document_title: str | None
    document_author: str | None
    detected_doi: str | None
    text: str


@dataclass(frozen=True)
class PaperMetadata:
    title: str
    authors: list[str]
    year: int | None
    doi: str | None
    category: str
    paper_type: str
    short_title: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_pdf_context(path: Path, max_pages: int = 8, max_chars: int = 30_000) -> ExtractedContext:
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise MetadataFailure("pdf_encrypted")
        metadata = reader.metadata
        parts: list[str] = []
        for page in reader.pages[:max_pages]:
            if sum(len(part) for part in parts) >= max_chars:
                break
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)[:max_chars]
        if not text.strip():
            raise MetadataFailure("pdf_text_unavailable")
        doi_match = DOI_PATTERN.search(text)
        return ExtractedContext(
            document_title=_clean_optional(getattr(metadata, "title", None)),
            document_author=_clean_optional(getattr(metadata, "author", None)),
            detected_doi=_normalize_doi(doi_match.group(0)) if doi_match else None,
            text=text,
        )
    except MetadataFailure:
        raise
    except Exception as exc:
        raise MetadataFailure("pdf_extract_failed") from exc


def _clean_optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def _normalize_doi(value: Any) -> str | None:
    cleaned = _clean_optional(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", cleaned, flags=re.I)
    match = DOI_PATTERN.search(cleaned)
    return match.group(0).rstrip(".,;)").lower() if match else None


def deepseek_api_key() -> str | None:
    direct = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if direct:
        return direct
    key_file = Path(os.environ.get("PAPER_DEEPSEEK_API_KEY_FILE", "/run/secrets/deepseek_api_key"))
    try:
        return key_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _prompt(context: ExtractedContext) -> list[dict[str, str]]:
    system = f"""You extract bibliographic metadata from research papers.
Return exactly one JSON object and no prose. Never invent unknown values.
category must be one of: {', '.join(CATEGORIES)}.
paper_type must be one of: {', '.join(PAPER_TYPES)}.
Use this JSON shape:
{{"title":"full title","authors":["author"],"year":2026,"doi":null,
"category":"Neuroscience","paper_type":"Research",
"short_title":"concise filename title","confidence":0.9}}
short_title must preserve the paper's meaning and be at most 90 characters."""
    user = json.dumps(
        {
            "embedded_title": context.document_title,
            "embedded_author": context.document_author,
            "detected_doi": context.detected_doi,
            "paper_excerpt": context.text,
        },
        ensure_ascii=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def classify_with_deepseek(context: ExtractedContext) -> PaperMetadata:
    api_key = deepseek_api_key()
    if not api_key:
        raise MetadataFailure("deepseek_not_configured")
    payload = {
        "model": os.environ.get("PAPER_DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "messages": _prompt(context),
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 1200,
    }
    endpoint = os.environ.get("PAPER_DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=int(os.environ.get("PAPER_DEEPSEEK_TIMEOUT_SECONDS", "120"))
        ) as response:
            envelope = json.load(response)
        content = envelope["choices"][0]["message"]["content"]
        if not content:
            raise MetadataFailure("deepseek_empty_response")
        return validate_metadata(json.loads(content), context)
    except MetadataFailure:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            code = "deepseek_auth_failed"
        elif exc.code == 429:
            code = "deepseek_rate_limited"
        else:
            code = f"deepseek_http_{exc.code}"
        raise MetadataFailure(code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MetadataFailure("deepseek_unavailable") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MetadataFailure("deepseek_invalid_response") from exc


def validate_metadata(data: Any, context: ExtractedContext) -> PaperMetadata:
    if not isinstance(data, dict):
        raise MetadataFailure("deepseek_invalid_response")
    title = _clean_optional(data.get("title")) or context.document_title
    short_title = _clean_optional(data.get("short_title")) or title
    raw_authors = data.get("authors")
    authors = []
    if isinstance(raw_authors, list):
        authors = [value for item in raw_authors if (value := _clean_optional(item))]
    if not authors and context.document_author:
        authors = [context.document_author]
    category = data.get("category")
    paper_type = data.get("paper_type")
    if not title or not short_title or category not in CATEGORIES or paper_type not in PAPER_TYPES:
        raise MetadataFailure("deepseek_invalid_response")
    raw_year = data.get("year")
    year = raw_year if isinstance(raw_year, int) and 1900 <= raw_year <= datetime.now(timezone.utc).year + 1 else None
    raw_confidence = data.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else 0.0
    if not 0 <= confidence <= 1:
        raise MetadataFailure("deepseek_invalid_response")
    return PaperMetadata(
        title=title[:500],
        authors=authors[:100],
        year=year,
        doi=_normalize_doi(data.get("doi")) or context.detected_doi,
        category=category,
        paper_type=paper_type,
        short_title=short_title[:90],
        confidence=confidence,
    )
