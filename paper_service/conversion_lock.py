"""Cross-container exclusive lock for memory-heavy paper conversions."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import Iterator


class ConversionLockFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _lock_path(value: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or len(value) > 1_000
        or path.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConversionLockFailure("conversion_lock_configuration_invalid")
    return path


@contextmanager
def exclusive_conversion_lock(
    path: str | Path,
    *,
    timeout_seconds: int,
    poll_seconds: float = 0.1,
) -> Iterator[None]:
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= 7_200
        or not isinstance(poll_seconds, (int, float))
        or isinstance(poll_seconds, bool)
        or not 0.01 <= poll_seconds <= 1.0
    ):
        raise ConversionLockFailure("conversion_lock_configuration_invalid")
    target = _lock_path(str(path))
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise ConversionLockFailure("conversion_lock_unavailable") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ConversionLockFailure("conversion_lock_unavailable")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ConversionLockFailure("conversion_lock_timeout") from exc
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def conversion_lock_from_env() -> Iterator[None]:
    path = os.environ.get(
        "PAPER_CONVERSION_LOCK_FILE",
        "/locks/heavy-conversion.lock",
    )
    try:
        timeout_seconds = int(
            os.environ.get("PAPER_CONVERSION_LOCK_TIMEOUT_SECONDS", "1900")
        )
    except ValueError as exc:
        raise ConversionLockFailure("conversion_lock_configuration_invalid") from exc
    with exclusive_conversion_lock(path, timeout_seconds=timeout_seconds):
        yield


__all__ = [
    "ConversionLockFailure",
    "conversion_lock_from_env",
    "exclusive_conversion_lock",
]
