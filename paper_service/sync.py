"""Google Drive copy command and twice-daily schedule helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class SyncFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScheduleSlot:
    key: str
    due_at: datetime


def parse_sync_times(raw: str) -> tuple[time, ...]:
    parsed: list[time] = []
    for value in raw.split(","):
        value = value.strip()
        try:
            parsed.append(time.fromisoformat(value))
        except ValueError as exc:
            raise ValueError("PAPER_SYNC_TIMES must contain HH:MM values") from exc
    if not parsed:
        raise ValueError("PAPER_SYNC_TIMES must not be empty")
    return tuple(sorted(set(parsed)))


def latest_due_slot(
    now: datetime,
    sync_times: tuple[time, ...],
    timezone_name: str,
) -> ScheduleSlot:
    timezone = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone)
    candidates: list[datetime] = []
    for day_offset in (0, -1):
        day = local_now.date() + timedelta(days=day_offset)
        candidates.extend(datetime.combine(day, value, timezone) for value in sync_times)
    due_at = max(candidate for candidate in candidates if candidate <= local_now)
    return ScheduleSlot(
        key=f"{due_at.date().isoformat()}T{due_at.strftime('%H:%M')}@{timezone_name}",
        due_at=due_at,
    )


def rclone_copy_command(incoming_dir: Path) -> list[str]:
    return [
        os.environ.get("PAPER_RCLONE_BIN", "rclone"),
        "copy",
        os.environ.get("PAPER_DRIVE_INBOX", "paper-drive:Paper Inbox"),
        str(incoming_dir),
        "--config",
        os.environ.get("PAPER_RCLONE_CONFIG", "/config/rclone/rclone.conf"),
        "--include",
        "*.[pP][dD][fF]",
        "--transfers",
        "2",
        "--checkers",
        "4",
        "--contimeout",
        "30s",
        "--timeout",
        "5m",
    ]


def copy_drive_inbox(incoming_dir: Path) -> None:
    incoming_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            rclone_copy_command(incoming_dir),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=int(os.environ.get("PAPER_RCLONE_TIMEOUT_SECONDS", "3600")),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailure("rclone_unavailable") from exc
    if result.returncode != 0:
        raise SyncFailure(f"rclone_exit_{result.returncode}")
