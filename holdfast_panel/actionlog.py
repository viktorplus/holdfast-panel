"""What the panel did, on which server, and how it ended.

One JSON object per line, appended. It never holds secrets: callers pass a
short description ("template holdfast-bootstrap"), not commands or values.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Entry:
    at: str
    server: str
    action: str
    ok: bool
    detail: str = ""


def record(
    path: Path,
    server: str,
    action: str,
    ok: bool,
    detail: str = "",
    now: datetime | None = None,
) -> None:
    entry = Entry(
        (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        server,
        action,
        ok,
        detail,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:  # several operations may finish at once
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_suffix(".1.jsonl"))  # one older file is kept
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")


def read(path: Path, limit: int = 200, server: str | None = None) -> list[Entry]:
    """The newest entries first. A damaged line is skipped, not fatal."""
    if not path.exists():
        return []
    entries = []
    # a damaged file must not stop the log from being read at all
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = Entry(**json.loads(line))
        except (ValueError, TypeError):
            continue
        if server is None or entry.server == server:
            entries.append(entry)
    return entries[::-1][:limit]
