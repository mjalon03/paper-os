import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_service.deepseek_client import (
    DeepSeekCompletion,
    DeepSeekTokenUsage,
)

from paper_service.metadata import (
    ExtractedContext,
    MetadataFailure,
    classify_with_deepseek,
    deepseek_api_key,
    validate_metadata,
)


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
            key_file.chmod(0o600)
            with patch.dict(
                os.environ,
                {"PAPER_DEEPSEEK_API_KEY_FILE": str(key_file)},
                clear=True,
            ):
                self.assertEqual(deepseek_api_key(), "test-key")

    def test_api_key_file_must_be_private_regular_and_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_file = root / "key"
            key_file.write_text("test-key\n")
            key_file.chmod(0o644)
            with patch.dict(
                os.environ,
                {"PAPER_DEEPSEEK_API_KEY_FILE": str(key_file)},
                clear=True,
            ):
                self.assertIsNone(deepseek_api_key())

            target = root / "target"
            target.write_text("test-key\n")
            target.chmod(0o600)
            key_file.unlink()
            key_file.symlink_to(target)
            with patch.dict(
                os.environ,
                {"PAPER_DEEPSEEK_API_KEY_FILE": str(key_file)},
                clear=True,
            ):
                self.assertIsNone(deepseek_api_key())

    @patch("paper_service.metadata.complete_deepseek_request")
    def test_deepseek_json_response_is_parsed(self, complete):
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
        complete.return_value = DeepSeekCompletion(
            content=json.dumps(metadata),
            usage=DeepSeekTokenUsage(100, 20, 120),
            request_sha256="a" * 64,
            response_sha256="b" * 64,
            provider_request_id="request-1",
        )
        api_key = "sk-" + "s" * 64
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": api_key}, clear=True):
            result = classify_with_deepseek(self.context)
        self.assertEqual(result.doi, "10.1000/test")
        body = complete.call_args.args[0]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertEqual(complete.call_args.kwargs["api_key"], api_key)


if __name__ == "__main__":
    unittest.main()
