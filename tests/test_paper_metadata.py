import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_service.metadata import (
    ExtractedContext,
    MetadataFailure,
    classify_with_deepseek,
    deepseek_api_key,
    validate_metadata,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return json.dumps(self.payload).encode()


class PaperMetadataTests(unittest.TestCase):
    def setUp(self):
        self.context = ExtractedContext(
            "Embedded title", "Ada Lovelace", "10.1000/test", "paper excerpt"
        )

    def test_validated_metadata_uses_detected_doi_fallback(self):
        result = validate_metadata(
            {
                "title": "A Brain Paper",
                "authors": ["Ada Lovelace", "Alan Turing"],
                "year": 2026,
                "doi": None,
                "category": "NeuroAI",
                "paper_type": "Research",
                "short_title": "A Brain Paper",
                "confidence": 0.95,
            },
            self.context,
        )
        self.assertEqual(result.doi, "10.1000/test")
        self.assertEqual(result.category, "NeuroAI")

    def test_invalid_category_fails_closed(self):
        with self.assertRaisesRegex(MetadataFailure, "deepseek_invalid_response"):
            validate_metadata(
                {
                    "title": "Paper",
                    "authors": [],
                    "year": 2026,
                    "category": "Invented Folder",
                    "paper_type": "Research",
                    "short_title": "Paper",
                    "confidence": 1,
                },
                self.context,
            )

    def test_api_key_file_is_supported_without_environment_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            key_file = Path(temporary) / "key"
            key_file.write_text("test-key\n")
            with patch.dict(
                os.environ,
                {"PAPER_DEEPSEEK_API_KEY_FILE": str(key_file)},
                clear=True,
            ):
                self.assertEqual(deepseek_api_key(), "test-key")

    @patch("paper_service.metadata.urllib.request.urlopen")
    def test_deepseek_json_response_is_parsed(self, urlopen):
        metadata = {
            "title": "A Brain Paper",
            "authors": ["Ada Lovelace"],
            "year": 2026,
            "doi": "https://doi.org/10.1000/TEST",
            "category": "Neuroscience",
            "paper_type": "Review",
            "short_title": "A Brain Paper",
            "confidence": 0.8,
        }
        urlopen.return_value = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(metadata)}}]}
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}, clear=True):
            result = classify_with_deepseek(self.context)
        self.assertEqual(result.doi, "10.1000/test")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        body = json.loads(request.data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("secret", request.data.decode())


if __name__ == "__main__":
    unittest.main()
