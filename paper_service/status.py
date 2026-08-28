"""Dependency-light health helpers for the paper service."""

from __future__ import annotations

import os
import socket
from typing import Any

from . import __version__


def service_status() -> dict[str, str]:
    return {
        "service": "paper-api",
        "status": "ok",
        "version": __version__,
    }


def database_target() -> tuple[str, int]:
    host = os.environ.get("PAPER_DB_HOST", "postgres")
    raw_port = os.environ.get("PAPER_DB_PORT", "5432")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("PAPER_DB_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PAPER_DB_PORT must be between 1 and 65535")
    return host, port


def database_reachable(timeout_seconds: float = 1.0) -> tuple[bool, str | None]:
    try:
        target = database_target()
        with socket.create_connection(target, timeout=timeout_seconds):
            return True, None
    except (OSError, ValueError) as exc:
        return False, str(exc)


def readiness_status() -> tuple[dict[str, Any], int]:
    reachable, _error = database_reachable()
    payload: dict[str, Any] = {
        "service": "paper-api",
        "status": "ready" if reachable else "not_ready",
        "checks": {"postgres": "ok" if reachable else "unavailable"},
    }
    return payload, 200 if reachable else 503
