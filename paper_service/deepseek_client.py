"""Bounded DeepSeek transport shared by reviewed Paper OS workers.

The transport accepts an already validated provider-neutral request body.  It
does not build prompts, persist source text, retry, or log response bodies.  A
worker records the returned token facts and decides whether a sanitized error
is retryable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import ssl
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
from urllib.parse import urlsplit


DEFAULT_ENDPOINT = "https://api.deepseek.com/chat/completions"
ALLOWED_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 2_000_000
_SAFE_REQUEST_ID_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}")


class DeepSeekTransportError(RuntimeError):
    """A sanitized provider or response failure."""

    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DeepSeekTokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class DeepSeekCompletion:
    content: str
    usage: DeepSeekTokenUsage
    request_sha256: str
    response_sha256: str
    provider_request_id: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, url):
        raise DeepSeekTransportError("deepseek_redirect_rejected", retryable=False)


def _fail(code: str, *, retryable: bool = False) -> DeepSeekTransportError:
    return DeepSeekTransportError(code, retryable=retryable)


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("deepseek_request_not_canonical_json") from exc
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise _fail("deepseek_request_size_invalid")
    return encoded


def _validate_endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise _fail("deepseek_endpoint_invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/chat/completions"
        or parsed.query
        or parsed.fragment
    ):
        raise _fail("deepseek_endpoint_invalid")
    return DEFAULT_ENDPOINT


def _validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise _fail("deepseek_api_key_invalid")
    key = value.strip()
    if not re.fullmatch(r"[!-~]{20,512}", key):
        raise _fail("deepseek_api_key_invalid")
    return key


def _validate_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("deepseek_request_invalid")
    payload = dict(value)
    model = payload.get("model")
    if model not in ALLOWED_MODELS:
        raise _fail("deepseek_model_not_allowed")
    if payload.get("stream") is not False:
        raise _fail("deepseek_streaming_forbidden")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 64:
        raise _fail("deepseek_messages_invalid")
    for item in messages:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "content"}
            or item.get("role") not in {"system", "user", "assistant"}
            or not isinstance(item.get("content"), str)
        ):
            raise _fail("deepseek_messages_invalid")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise _fail("deepseek_response_duplicate_key")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise _fail(f"deepseek_response_non_finite_{value}")


def _token_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail(f"deepseek_usage_{field}_invalid")
    return value


def _parse_response(raw: bytes, *, request_sha256: str, request_id: Any) -> DeepSeekCompletion:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise _fail("deepseek_response_size_invalid")
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except DeepSeekTransportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _fail("deepseek_response_invalid_json") from exc
    try:
        choices = envelope["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise KeyError("choices")
        content = choices[0]["message"]["content"]
        usage_value = envelope["usage"]
        if not isinstance(content, str) or not content.strip():
            raise KeyError("content")
        if not isinstance(usage_value, dict):
            raise KeyError("usage")
    except (KeyError, IndexError, TypeError) as exc:
        raise _fail("deepseek_response_shape_invalid") from exc
    if len(content) > MAX_RESPONSE_BYTES:
        raise _fail("deepseek_response_content_too_large")
    prompt = _token_count(usage_value.get("prompt_tokens"), field="prompt_tokens")
    completion = _token_count(
        usage_value.get("completion_tokens"), field="completion_tokens"
    )
    total = _token_count(usage_value.get("total_tokens"), field="total_tokens")
    if total != prompt + completion:
        raise _fail("deepseek_usage_total_tokens_mismatch")
    safe_request_id = (
        request_id.strip()
        if isinstance(request_id, str)
        and _SAFE_REQUEST_ID_RE.fullmatch(request_id.strip())
        else None
    )
    return DeepSeekCompletion(
        content=content,
        usage=DeepSeekTokenUsage(prompt, completion, total),
        request_sha256=request_sha256,
        response_sha256=hashlib.sha256(raw).hexdigest(),
        provider_request_id=safe_request_id,
    )


def _default_open(request: urllib.request.Request, timeout: int):
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    return opener.open(request, timeout=timeout)


def complete_deepseek_request(
    payload: Mapping[str, Any],
    *,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout_seconds: int = 120,
    open_request: Callable[[urllib.request.Request, int], Any] = _default_open,
) -> DeepSeekCompletion:
    """Perform one non-streaming completion with strict, bounded parsing."""

    clean_endpoint = _validate_endpoint(endpoint)
    clean_key = _validate_api_key(api_key)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 300
    ):
        raise _fail("deepseek_timeout_invalid")
    body = _canonical_json(_validate_payload(payload))
    request_sha256 = hashlib.sha256(body).hexdigest()
    request = urllib.request.Request(
        clean_endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {clean_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "paper-os/0.6",
        },
        method="POST",
    )
    try:
        with open_request(request, timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    length = int(content_length)
                except ValueError as exc:
                    raise _fail("deepseek_content_length_invalid") from exc
                if length < 1 or length > MAX_RESPONSE_BYTES:
                    raise _fail("deepseek_response_size_invalid")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            request_id = response.headers.get("X-Request-Id")
    except DeepSeekTransportError:
        raise
    except urllib.error.HTTPError as exc:
        retryable = exc.code == 429 or 500 <= exc.code <= 599
        code = (
            "deepseek_auth_failed"
            if exc.code in {401, 403}
            else "deepseek_rate_limited"
            if exc.code == 429
            else "deepseek_provider_error"
        )
        raise _fail(code, retryable=retryable) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _fail("deepseek_unavailable", retryable=True) from exc
    return _parse_response(
        raw,
        request_sha256=request_sha256,
        request_id=request_id,
    )


__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_ENDPOINT",
    "DeepSeekCompletion",
    "DeepSeekTokenUsage",
    "DeepSeekTransportError",
    "complete_deepseek_request",
]
