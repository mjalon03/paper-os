import os
import unittest
from unittest.mock import patch

from paper_service.database import database_config
from paper_service.status import database_target, readiness_status, service_status


class PaperServiceStatusTests(unittest.TestCase):
    def test_service_status_is_stable(self):
        self.assertEqual(
            service_status(),
            {"service": "paper-api", "status": "ok", "version": "0.4.0"},
        )

    def test_database_target_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(database_target(), ("postgres", 5432))

    def test_database_target_rejects_invalid_port(self):
        with patch.dict(os.environ, {"PAPER_DB_PORT": "invalid"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                database_target()

    def test_database_config_preserves_password_characters(self):
        with patch.dict(
            os.environ,
            {"PAPER_DB_PASSWORD": "spaces and ' punctuation"},
            clear=True,
        ):
            self.assertEqual(database_config()["password"], "spaces and ' punctuation")

    def test_database_config_requires_password(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must be set"):
                database_config()

    @patch("paper_service.status.database_reachable", return_value=(True, None))
    def test_readiness_succeeds_when_postgres_is_reachable(self, _reachable):
        payload, status_code = readiness_status()
        self.assertEqual(status_code, 200)
        self.assertEqual(payload["checks"], {"postgres": "ok"})

    @patch(
        "paper_service.status.database_reachable",
        return_value=(False, "connection refused"),
    )
    def test_readiness_fails_closed(self, _reachable):
        payload, status_code = readiness_status()
        self.assertEqual(status_code, 503)
        self.assertEqual(payload["status"], "not_ready")
        self.assertNotIn("detail", payload)


if __name__ == "__main__":
    unittest.main()
