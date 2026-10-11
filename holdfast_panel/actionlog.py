"""What the panel did, on which server, and how it ended.

One JSON object per line, appended. It never holds secrets: callers pass a
short description ("template holdfast-bootstrap"), not commands or values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")


def read(path: Path, limit: int = 200, server: str | None = None) -> list[Entry]:
    """The newest entries first. A damaged line is skipped, not fatal."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = Entry(**json.loads(line))
        except (ValueError, TypeError):
            continue
        if server is None or entry.server == server:
            entries.append(entry)
    return entries[::-1][:limit]
