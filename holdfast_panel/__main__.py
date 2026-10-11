from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .context import AppContext
from .paths import data_dir
from .ui.main_window import MainWindow


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow(AppContext.create(data_dir()))
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
