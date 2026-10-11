"""What every part of the window shares: stores, the key, and how to talk to a server.

The dialogs and the connection are attributes, so a test can replace them
with something that answers at once instead of opening a window or a socket.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from . import actionlog
from .keys import CredentialManager, KeyPair, ensure_keypair
from .library import Library
from .ssh import Connection, connect
from .store import Server, ServerStore, StorageStore


@dataclass
class AppContext:
    data_dir: Path
    servers: ServerStore
    storages: StorageStore
    library: Library
    get_key: Callable[[], KeyPair]
    pool: QThreadPool = field(default_factory=QThreadPool.globalInstance)
    open_connection: Callable[[Server], Connection] | None = None
    show_error: Callable[[str], None] = lambda message: None
    confirm: Callable[[str, str], bool] = lambda title, text: False
    ask_key_file: Callable[[], Path | None] = lambda: None
    """Which private key file to use for this operation (never remembered on disk)."""
    choose: Callable[[str, list[str]], int | None] = lambda title, options: None
    _parent: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.open_connection is None:
            self.open_connection = lambda server: connect(server, key=self.get_key())

    @property
    def parent_window(self):
        """The main window, held weakly: the window holds this context, and a
        strong reference back would keep both alive past the QApplication."""
        return self._parent() if self._parent else None

    @parent_window.setter
    def parent_window(self, window) -> None:
        self._parent = weakref.ref(window) if window is not None else None

    @property
    def log_path(self) -> Path:
        return self.data_dir / "actions.jsonl"

    def log(
        self, server: Server | str, action: str, ok: bool, detail: str = ""
    ) -> None:
        name = server if isinstance(server, str) else server.name
        actionlog.record(self.log_path, name, action, ok, detail)

    @classmethod
    def create(cls, data_dir: Path) -> AppContext:
        context = cls(
            data_dir=data_dir,
            servers=ServerStore(data_dir / "servers.json"),
            storages=StorageStore(data_dir / "storages.json"),
            library=Library(data_dir / "templates"),
            get_key=_cached_key(data_dir),
        )
        context.show_error = lambda message: QMessageBox.warning(
            context.parent_window, "Не получилось", message
        )
        context.ask_key_file = lambda: _pick_key_file(context.parent_window)
        context.choose = lambda title, options: _pick_one(
            context.parent_window, title, options
        )
        context.confirm = lambda title, text: (
            QMessageBox.question(
                context.parent_window,
                title,
                text,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,  # Enter must not overwrite live data
            )
            == QMessageBox.StandardButton.Yes
        )
        return context


def _pick_key_file(parent) -> Path | None:
    name, _ = QFileDialog.getOpenFileName(
        parent,
        "Файл приватного ключа шифрования (age)",
        "",
        "Ключ (*.txt *.key);;Все файлы (*)",
    )
    return Path(name) if name else None


def _pick_one(parent, title: str, options: list[str]) -> int | None:
    item, ok = QInputDialog.getItem(parent, title, title, options, 0, False)
    return options.index(item) if ok else None


def _cached_key(data_dir: Path) -> Callable[[], KeyPair]:
    """The key is opened once per run: opening it derives a key from the
    passphrase, which is slow, and every connection of every tab asks."""
    lock = threading.Lock()
    cache: list[KeyPair] = []

    def get() -> KeyPair:
        with lock:
            if not cache:
                cache.append(ensure_keypair(data_dir, CredentialManager()))
            return cache[0]

    return get
