from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from paper_service.vault_bridge_cli import main


class VaultBridgeCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.ledger_parent = self.root / "state"
        self.ledger_parent.mkdir()
        self.ledger = self.ledger_parent / "ledger.json"
        self.client_id = self.root / "client-id"
        self.secret = self.root / "secret"
        self.client_id.write_text("a" * 32 + ".access\n")
        self.secret.write_text("s" * 64 + "\n")
        self.client_id.chmod(0o600)
        self.secret.chmod(0o600)

    def arguments(self, command="check"):
        return [
            "--client-id-file",
            str(self.client_id),
            "--client-secret-file",
            str(self.secret),
            "--consumer-id",
            "mac-mini",
            "--vault-root",
            str(self.vault),
            "--ledger",
            str(self.ledger),
            command,
        ]

    def test_check_is_network_free_and_write_free(self):
        output = StringIO()
        with patch("urllib.request.urlopen") as network, redirect_stdout(output):
            code = main(self.arguments())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "configuration-valid")
        network.assert_not_called()
        self.assertFalse(self.ledger.exists())
        self.assertFalse((self.vault / "Research").exists())

    def test_sync_requires_explicit_apply_before_network_or_writes(self):
        error = StringIO()
        with patch("urllib.request.urlopen") as network, redirect_stderr(error):
            code = main(self.arguments("sync-once"))
        self.assertEqual(code, 2)
        self.assertIn("apply_required", error.getvalue())
        network.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_ledger_must_be_outside_the_vault(self):
        arguments = self.arguments()
        arguments[arguments.index(str(self.ledger))] = str(self.vault / "ledger.json")
        with redirect_stderr(StringIO()):
            self.assertEqual(main(arguments), 2)


if __name__ == "__main__":
    unittest.main()
