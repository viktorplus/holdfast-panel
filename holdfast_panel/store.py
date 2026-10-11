"""The list of servers, as one JSON file.

Nothing secret is written here: a server is an address, a login and the
fingerprint of its host key. The password is used once and never stored.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Server:
    name: str
    host: str
    port: int = 22
    user: str = "root"
    host_fingerprint: str = ""
    """SHA256 fingerprint the user accepted. Empty until the first contact."""
    enrolled: bool = False
    """True once the panel's key is on the server and a login with it worked."""
    backup_root: str = "/opt/backups"
    """Where holdfast keeps this server's snapshots (`backup.root`)."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class StoreError(Exception):
    pass


SPEC_PATTERN = r"[A-Za-z0-9_.-]+:[^\s'\"`$\;|&<>]*"
"""An rclone path, `remote:folder`: no whitespace, quotes or shell characters."""


@dataclass
class Storage:
    """A place snapshots can be kept besides the server itself."""

    name: str
    kind: str
    """"pc": a folder on this computer. "remote": an rclone path, `remote:folder`."""
    location: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class _ListStore:
    """A JSON file holding one list of records, each with a unique name and id."""

    cls: type
    key: str
    what: str

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [self.cls(**row) for row in raw[self.key]]
        except (ValueError, KeyError, TypeError) as exc:
            # Not silently an empty list: the next save would overwrite a file
            # that is only damaged, and the user would lose the whole list.
            raise StoreError(f"{self.path} cannot be read: {exc}") from exc

    def save(self, records: list) -> None:
        """Write the whole list: to a temporary file of its own, then replace.

        The temporary name is unique, so two writers never share one; and the
        replace is retried a few times, because on Windows a virus scanner or the
        search indexer may hold the target open for a moment.
        """
        body = json.dumps({self.key: [asdict(r) for r in records]}, indent=2)
        handle, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(body)
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def check(self, record) -> None:
        """Raise StoreError if the record must not be stored."""

    def add(self, record) -> None:
        records = self.load()
        if any(r.name == record.name for r in records):
            raise StoreError(f"a {self.what} called {record.name!r} already exists")
        self.check(record)
        self.save([*records, record])

    def update(self, record) -> None:
        records = self.load()
        if not any(r.id == record.id for r in records):
            raise StoreError(f"no {self.what} with this id; it was removed meanwhile")
        if any(r.name == record.name and r.id != record.id for r in records):
            raise StoreError(f"a {self.what} called {record.name!r} already exists")
        self.check(record)
        self.save([record if r.id == record.id else r for r in records])

    def remove(self, record_id: str) -> None:
        self.save([r for r in self.load() if r.id != record_id])


class ServerStore(_ListStore):
    cls, key, what = Server, "servers", "server"


class StorageStore(_ListStore):
    cls, key, what = Storage, "storages", "storage"

    def check(self, record) -> None:
        if record.kind not in ("pc", "remote"):
            raise StoreError(f"kind must be pc or remote, not {record.kind!r}")
        if not record.name.strip() or not record.location.strip():
            raise StoreError("a storage needs a name and a location")
        if record.kind == "remote" and not re.fullmatch(SPEC_PATTERN, record.location):
            raise StoreError(
                "a remote location looks like remote:folder, without spaces or quotes"
            )
