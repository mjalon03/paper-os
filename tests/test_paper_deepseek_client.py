from __future__ import annotations

import io
import json
import unittest
import urllib.error

from paper_service.deepseek_client import (
    MAX_RESPONSE_BYTES,
    DeepSeekTransportError,
    complete_deepseek_request,
)


API_KEY = "sk-" + "s" * 64


def payload() -> dict:
    return {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "untrusted source"},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "temperature": 0,
        "max_tokens": 512,
    }


def response_bytes() -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": '{"status":"ok"}'}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int) -> bytes:
        return self.body.read(size)


class DeepSeekTransportTests(unittest.TestCase):
    def test_valid_response_returns_content_usage_and_digests(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["timeout"] = timeout
            captured["body"] = request.data
            captured["authorization"] = request.get_header("Authorization")
            return FakeResponse(
                response_bytes(),
                {"Content-Length": str(len(response_bytes())), "X-Request-Id": "r-7"},
            )

        result = complete_deepseek_request(
            payload(), api_key=API_KEY, open_request=open_request
        )

        self.assertEqual(result.content, '{"status":"ok"}')
        self.assertEqual(result.usage.total_tokens, 120)
        self.assertRegex(result.request_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(result.response_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(result.provider_request_id, "r-7")
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 120)
        self.assertEqual(captured["authorization"], f"Bearer {API_KEY}")
        self.assertEqual(json.loads(captured["body"]), payload())

    def test_endpoint_model_streaming_timeout_and_key_are_fail_closed(self):
        cases = [
            ({"endpoint": "http://api.deepseek.com/chat/completions"}, "endpoint"),
            ({"endpoint": "https://attacker.invalid/chat/completions"}, "endpoint"),
            ({"api_key": "short"}, "api_key"),
            ({"timeout_seconds": 0}, "timeout"),
        ]
        for kwargs, code in cases:
            arguments = {"api_key": API_KEY, **kwargs}
            with self.subTest(code=code):
                with self.assertRaisesRegex(DeepSeekTransportError, code):
                    complete_deepseek_request(payload(), **arguments)

        invalid_model = payload()
        invalid_model["model"] = "unknown-model"
        with self.assertRaisesRegex(DeepSeekTransportError, "model_not_allowed"):
            complete_deepseek_request(invalid_model, api_key=API_KEY)
        streaming = payload()
        streaming["stream"] = True
        with self.assertRaisesRegex(DeepSeekTransportError, "streaming_forbidden"):
            complete_deepseek_request(streaming, api_key=API_KEY)

    def test_http_and_network_failures_are_sanitized_and_classified(self):
        for status, expected, retryable in (
            (401, "auth_failed", False),
            (429, "rate_limited", True),
            (503, "provider_error", True),
        ):
            def fail(_request, _timeout, status=status):
                raise urllib.error.HTTPError(
                    "https://api.deepseek.com/chat/completions",
                    status,
                    "provider details with secret " + API_KEY,
                    {},
                    None,
                )

            with self.subTest(status=status):
                with self.assertRaisesRegex(DeepSeekTransportError, expected) as caught:
                    complete_deepseek_request(
                        payload(), api_key=API_KEY, open_request=fail
                    )
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn(API_KEY, str(caught.exception))

        def offline(_request, _timeout):
            raise urllib.error.URLError("network detail " + API_KEY)

        with self.assertRaisesRegex(DeepSeekTransportError, "unavailable") as caught:
            complete_deepseek_request(payload(), api_key=API_KEY, open_request=offline)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_response_is_bounded_strict_json_and_usage_consistent(self):
        cases = [
            (b"", "response_size_invalid"),
            (b"x" * (MAX_RESPONSE_BYTES + 1), "response_size_invalid"),
            (b"not json", "invalid_json"),
            (
                b'{"choices":[],"usage":{"prompt_tokens":1,'
                b'"completion_tokens":1,"total_tokens":2}}',
                "shape_invalid",
            ),
            (
                b'{"choices":[{"message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1,'
                b'"total_tokens":3}}',
                "total_tokens_mismatch",
            ),
            (
                b'{"choices":[],"choices":[],"usage":{}}',
                "duplicate_key",
            ),
        ]
        for body, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(DeepSeekTransportError, code):
                    complete_deepseek_request(
                        payload(),
                        api_key=API_KEY,
                        open_request=lambda _request, _timeout, body=body: FakeResponse(body),
                    )

    def test_invalid_content_length_is_rejected_before_body_parsing(self):
        for value in ("not-a-number", str(MAX_RESPONSE_BYTES + 1), "0"):
            with self.subTest(value=value):
                with self.assertRaises(DeepSeekTransportError):
                    complete_deepseek_request(
                        payload(),
                        api_key=API_KEY,
                        open_request=lambda _request, _timeout, value=value: FakeResponse(
                            response_bytes(), {"Content-Length": value}
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
