"""Append-only JSONL run logs shared by the harness modules."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Clock = Callable[[], datetime]


class UtcClock:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def iso(clock: Clock | None = None) -> str:
        moment = (clock or UtcClock.now)()
        if moment.tzinfo is None:
            raise ValueError("run log timestamps must be timezone-aware")
        return moment.astimezone(timezone.utc).isoformat()


class JsonlAppendLog:
    """One JSON object per line. Records are only ever appended, never rewritten."""

    _lock = threading.Lock()

    @staticmethod
    def append(path: str | Path, record: dict[str, Any]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
        with JsonlAppendLog._lock:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def read(path: str | Path) -> list[dict[str, Any]]:
        target = Path(path)
        if not target.exists():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"run log line {number} is not a JSON object")
            records.append(record)
        return records
