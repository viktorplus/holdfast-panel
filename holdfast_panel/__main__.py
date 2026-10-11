from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .keys import CredentialManager, ensure_keypair
from .paths import data_dir
from .store import ServerStore
from .ui.main_window import MainWindow


def run() -> None:
    app = QApplication(sys.argv)
    directory = data_dir()
    store = ServerStore(directory / "servers.json")

    def get_key():
        return ensure_keypair(directory, CredentialManager())

    window = MainWindow(store, get_key)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
