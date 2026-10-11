from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
)

from ..enroll import enroll
from ..keys import KeyPair
from ..ssh import connect, fetch_host_fingerprint
from ..store import Server, ServerStore, StoreError
from .add_dialog import AddServerDialog
from .workers import Task

COLUMNS = ["Имя", "Адрес", "Пользователь", "Состояние", "holdfast"]
VERSION_COMMAND = "command -v holdfast >/dev/null && holdfast version"


def probe(server: Server, key: KeyPair) -> str:
    """The version of holdfast on the server, or '' if it is not installed."""
    with connect(server, key=key) as session:
        result = session.run(VERSION_COMMAND)
        return result.out.strip() if result.ok else ""


class MainWindow(QMainWindow):
    def __init__(self, store: ServerStore, get_key: Callable[[], KeyPair]):
        super().__init__()
        self.store = store
        self.get_key = get_key
        self.pool = QThreadPool.globalInstance()
        self.setWindowTitle("Holdfast Panel")
        self.resize(900, 460)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().hide()
        self.setCentralWidget(self.table)

        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        for text, slot in (
            ("Добавить сервер", self.add_server),
            ("Удалить", self.remove_server),
            ("Обновить состояние", self.refresh),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            bar.addAction(action)

        self.servers: list[Server] = []
        self.reload()
        self.refresh()

    # ---- the list ---------------------------------------------------------

    def reload(self) -> None:
        try:
            self.servers = self.store.load()
        except StoreError as exc:
            QMessageBox.critical(self, "Список серверов", str(exc))
            self.servers = []
        self.table.setRowCount(len(self.servers))
        for row, server in enumerate(self.servers):
            cells = [
                server.name,
                f"{server.host}:{server.port}",
                server.user,
                "ключ установлен" if server.enrolled else "ключ не установлен",
                "",
            ]
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))

    def selected(self) -> Server | None:
        row = self.table.currentRow()
        return self.servers[row] if 0 <= row < len(self.servers) else None

    def set_status(self, server: Server, state: str, version: str = "") -> None:
        for row, current in enumerate(self.servers):
            if current.id == server.id:
                self.table.item(row, 3).setText(state)
                self.table.item(row, 4).setText(version)

    # ---- add --------------------------------------------------------------

    def add_server(self) -> None:
        dialog = AddServerDialog(self)
        if dialog.exec() != AddServerDialog.DialogCode.Accepted:
            return
        name, host, port, user, password = dialog.values()
        server = Server(name=name, host=host, port=port, user=user)
        self.statusBar().showMessage(f"Связываюсь с {host}…")
        task = Task(lambda: fetch_host_fingerprint(host, port))
        task.signals.done.connect(lambda fp: self._confirm_host(server, fp, password))
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _confirm_host(self, server: Server, fingerprint: str, password: str) -> None:
        self.statusBar().clearMessage()
        answer = QMessageBox.question(
            self,
            "Отпечаток ключа сервера",
            f"Сервер {server.host}:{server.port} представился отпечатком:\n\n"
            f"{fingerprint}\n\n"
            "Сверьте его с панелью хостинга или консолью сервера. "
            "Подключаться, только если он совпадает.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        server.host_fingerprint = fingerprint
        try:
            self.store.add(server)
        except StoreError as exc:
            self._failed(str(exc))
            return
        self.reload()
        self.statusBar().showMessage("Устанавливаю ключ…")
        task = Task(lambda: enroll(server, password, self.get_key()))
        task.signals.done.connect(lambda _: self._enrolled(server))
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _enrolled(self, server: Server) -> None:
        server.enrolled = True
        self.store.update(server)
        self.statusBar().showMessage(f"{server.name}: ключ установлен", 5000)
        self.reload()
        self.check(server)

    # ---- remove and refresh ----------------------------------------------

    def remove_server(self) -> None:
        server = self.selected()
        if server is None:
            return
        answer = QMessageBox.question(
            self,
            "Удалить",
            f"Убрать «{server.name}» из списка? Сам сервер и ключ панели на нём "
            "не меняются.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.store.remove(server.id)
            self.reload()

    def refresh(self) -> None:
        for server in self.servers:
            if server.enrolled:
                self.check(server)

    def check(self, server: Server) -> None:
        self.set_status(server, "проверяю…")
        task = Task(lambda: probe(server, self.get_key()))
        task.signals.done.connect(
            lambda version: self.set_status(
                server,
                "в сети",
                version or "holdfast не установлен",
            )
        )
        task.signals.failed.connect(
            lambda message: self.set_status(server, f"нет связи: {message}")
        )
        self.pool.start(task)

    def _failed(self, message: str) -> None:
        self.statusBar().clearMessage()
        QMessageBox.warning(self, "Не получилось", message)
