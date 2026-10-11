"""The list of servers, as one JSON file.

Nothing secret is written here: a server is an address, a login and the
fingerprint of its host key. The password is used once and never stored.
"""

from __future__ import annotations

import json
import os
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
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class StoreError(Exception):
    pass


class ServerStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[Server]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [Server(**row) for row in raw["servers"]]
        except (ValueError, KeyError, TypeError) as exc:
            # Not silently an empty list: the next save would overwrite a file
            # that is only damaged, and the user would lose the whole list.
            raise StoreError(f"{self.path} cannot be read: {exc}") from exc

    def save(self, servers: list[Server]) -> None:
        body = json.dumps({"servers": [asdict(s) for s in servers]}, indent=2)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, server: Server) -> None:
        servers = self.load()
        if any(s.name == server.name for s in servers):
            raise StoreError(f"a server called {server.name!r} already exists")
        self.save([*servers, server])

    def update(self, server: Server) -> None:
        servers = self.load()
        self.save([server if s.id == server.id else s for s in servers])

    def remove(self, server_id: str) -> None:
        self.save([s for s in self.load() if s.id != server_id])
