"""Explicit Mac CLI for one reviewed-knowledge outbox delivery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .vault_bridge_client import (
    DEFAULT_ORIGIN,
    VaultBridgeClient,
    VaultBridgeError,
    load_service_credentials,
    sync_reviewed_knowledge_once,
)
from .vault_sync import VaultSyncWriter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-vault-bridge")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--client-id-file", type=Path, required=True)
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument("--consumer-id", required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="validate configuration without network or writes")
    sync = subparsers.add_parser("sync-once", help="deliver at most one reviewed revision")
    sync.add_argument(
        "--apply",
        action="store_true",
        help="required before leasing or writing reviewed knowledge",
    )
    return parser


def _print(value: dict[str, object]) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _validate_local_paths(vault_root: Path, ledger: Path) -> None:
    if vault_root.is_symlink() or not vault_root.is_dir():
        raise VaultBridgeError("vault_root_invalid")
    if not ledger.is_absolute() or ledger.is_symlink():
        raise VaultBridgeError("ledger_path_invalid")
    if ledger.parent.is_symlink() or not ledger.parent.is_dir():
        raise VaultBridgeError("ledger_parent_invalid")
    try:
        ledger.relative_to(vault_root)
    except ValueError:
        return
    raise VaultBridgeError("ledger_must_be_outside_vault")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        credentials = load_service_credentials(
            arguments.client_id_file, arguments.client_secret_file
        )
        _validate_local_paths(arguments.vault_root, arguments.ledger)
        client = VaultBridgeClient(
            credentials=credentials,
            consumer_id=arguments.consumer_id,
            origin=arguments.origin,
        )
        if arguments.command == "check":
            _print({"mode": "check", "status": "configuration-valid"})
            return 0
        if not arguments.apply:
            raise VaultBridgeError("apply_required")
        writer = VaultSyncWriter(arguments.vault_root, arguments.ledger)
        result = sync_reviewed_knowledge_once(client, writer)
        # Scheduled logs contain no paper title, note filename, source text, or secret.
        _print(
            {
                "status": result.status,
                "outbox_id": result.outbox_id,
                "revision_id": result.revision_id,
                "import_result": result.import_result,
            }
        )
        return 0
    except VaultBridgeError as exc:
        print(f"paper-vault-bridge: {exc.code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
