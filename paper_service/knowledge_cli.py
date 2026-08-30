"""Manual Mac-side bridge for canonical paper-knowledge JSON files.

The default import mode is a dry run.  ``--apply`` is required before the
fixed ``Research/Papers`` subtree or the local ledger can be changed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Sequence

from .knowledge import (
    KnowledgeValidationError,
    load_knowledge_payload,
    payload_sha256,
    render_knowledge_markdown,
)
from .vault_sync import VaultSyncError, VaultSyncWriter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-knowledge",
        description="Validate, render, or safely import canonical paper knowledge.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate canonical JSON")
    validate.add_argument("payload", type=Path)

    render = subparsers.add_parser("render", help="render Markdown without writes")
    render.add_argument("payload", type=Path)

    importer = subparsers.add_parser(
        "import", help="preview by default; write only when --apply is present"
    )
    importer.add_argument("payload", type=Path)
    importer.add_argument("--vault-root", type=Path, required=True)
    importer.add_argument("--ledger", type=Path, required=True)
    importer.add_argument(
        "--apply",
        action="store_true",
        help="perform the create-only vault import",
    )
    return parser


def _json_line(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        payload = load_knowledge_payload(arguments.payload)
        if arguments.command == "validate":
            _json_line(
                {
                    "payload_sha256": payload_sha256(payload),
                    "revision_id": payload["revision_id"],
                    "schema_version": payload["schema_version"],
                    "selector_version": payload["selector_version"],
                    "status": "valid",
                    "validation_scope": "structural; source proof is server-owned",
                }
            )
            return 0
        if arguments.command == "render":
            sys.stdout.write(render_knowledge_markdown(payload))
            return 0

        if not arguments.apply:
            writer = VaultSyncWriter(arguments.vault_root, arguments.ledger)
            _json_line({"mode": "dry-run", **writer.preflight(payload)})
            return 0
        writer = VaultSyncWriter(
            arguments.vault_root,
            arguments.ledger,
        )
        _json_line({"mode": "apply", **asdict(writer.import_payload(payload))})
        return 0
    except (KnowledgeValidationError, VaultSyncError) as exc:
        print(f"paper-knowledge: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("paper-knowledge: filesystem_operation_failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
