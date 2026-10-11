from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
)

from ..alerts import evaluate, worst
from ..context import AppContext
from ..diagnostics import FAIL, OK, WARN
from ..enroll import enroll
from ..monitor import collect
from ..ssh import fetch_host_fingerprint
from ..store import Server, StoreError
from .add_dialog import AddServerDialog
from .backups_tab import BackupsTab
from .base import ServerTab
from .contents_tab import ContentsTab
from .diagnostics_tab import DiagnosticsTab
from .log_tab import LogTab
from .monitor_tab import MonitorTab
from .templates_tab import TemplatesTab
from .workers import Task

COLUMNS = ["Имя", "Адрес", "Состояние"]
MARK = {OK: "●", WARN: "▲", FAIL: "✖"}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        ctx.parent_window = self
        self.setWindowTitle("Holdfast Panel")
        self.resize(1200, 720)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self._sync_tabs)

        self.tabs = QTabWidget()
        self.tab_list: list[ServerTab] = []
        self.add_tab(MonitorTab(ctx), "Монитор")
        self.add_tab(BackupsTab(ctx), "Бэкапы")
        self.add_tab(ContentsTab(ctx), "Состав архива")
        self.add_tab(TemplatesTab(ctx), "Шаблоны")
        self.add_tab(DiagnosticsTab(ctx), "Диагностика")
        self.add_tab(LogTab(ctx), "Журнал")
        self.tabs.currentChanged.connect(self._refresh_current)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([430, 770])
        self.setCentralWidget(splitter)

        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)
        for text, slot in (
            ("Добавить сервер", self.add_server),
            ("Удалить", self.remove_server),
            ("Обновить состояние", self.refresh_all),
        ):
            action = QAction(text, self)
            action.triggered.connect(slot)
            bar.addAction(action)

        self._checking: set[str] = set()
        self.servers: list[Server] = []
        self.reload()
        self.refresh_all()

    def closeEvent(self, event) -> None:
        busy = [t for t in self.tab_list if getattr(t, "busy", False)]
        if busy and not self.ctx.confirm(
            "Закрыть панель",
            "Операция ещё выполняется на сервере. Если закрыть панель сейчас, "
            "она может остановиться на середине. Закрыть?",
        ):
            event.ignore()
            return
        for tab in self.tab_list:
            if hasattr(tab, "closing"):
                tab.closing = True  # a worker waiting for an answer stops waiting
        event.accept()

    def add_tab(self, tab: ServerTab, title: str) -> None:
        self.tab_list.append(tab)
        self.tabs.addTab(tab, title)

    # ---- the list ---------------------------------------------------------

    def reload(self, keep: str | None = None) -> None:
        try:
            self.servers = self.ctx.servers.load()
        except StoreError as exc:
            self.ctx.show_error(str(exc))
            self.servers = []
        self.table.setRowCount(len(self.servers))
        for row, server in enumerate(self.servers):
            state = "в сети?" if server.enrolled else "ключ не установлен"
            for column, text in enumerate(
                [server.name, f"{server.host}:{server.port}", state]
            ):
                self.table.setItem(row, column, QTableWidgetItem(text))
            if keep == server.id:
                self.table.selectRow(row)

    def selected(self) -> Server | None:
        row = self.table.currentRow()
        return self.servers[row] if 0 <= row < len(self.servers) else None

    def set_state(self, server: Server, text: str) -> None:
        for row, current in enumerate(self.servers):
            if current.id == server.id:
                self.table.item(row, 2).setText(text)
                self.table.item(row, 2).setToolTip(text)  # the column cuts long text

    def _sync_tabs(self) -> None:
        server = self.selected()
        for tab in self.tab_list:
            tab.server = server
        self._refresh_current()

    def _refresh_current(self, *_: object) -> None:
        tab = self.tab_list[self.tabs.currentIndex()]
        tab.refresh()

    # ---- add --------------------------------------------------------------

    def add_server(self) -> None:
        dialog = AddServerDialog(self)
        accepted = dialog.exec() == AddServerDialog.DialogCode.Accepted
        name, host, port, user, password = dialog.values()
        dialog.password.clear()  # the form must not keep it
        dialog.deleteLater()
        if not accepted:
            return
        box = [password]  # emptied once used; closures hold the box, not the text
        server = Server(name=name, host=host, port=port, user=user)
        self.statusBar().showMessage(f"Связываюсь с {host}…")
        task = Task(lambda: fetch_host_fingerprint(host, port))
        task.signals.done.connect(lambda fp: self._confirm_host(server, fp, box))
        task.signals.failed.connect(lambda message: self._enrol_failed(message, box))
        self._keep(task)

    def _enrol_failed(self, message: str, box: list[str]) -> None:
        box[0] = ""
        self._failed(message)

    def _keep(self, task: Task) -> None:
        self.ctx.pool.start(task)
        self._running = [*getattr(self, "_running", []), task]
        task.signals.done.connect(lambda _: self._running.remove(task))
        task.signals.failed.connect(lambda _: self._running.remove(task))

    def _confirm_host(self, server: Server, fingerprint: str, box: list[str]) -> None:
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
            box[0] = ""
            return
        server.host_fingerprint = fingerprint
        try:
            # A server whose key never got installed (wrong password, link lost)
            # is added again under the same name: it is updated, not refused.
            unfinished = next(
                (
                    s
                    for s in self.ctx.servers.load()
                    if s.name == server.name and not s.enrolled
                ),
                None,
            )
            if unfinished is not None:
                server.id = unfinished.id
                self.ctx.servers.update(server)
            else:
                self.ctx.servers.add(server)
        except StoreError as exc:
            self._enrol_failed(str(exc), box)
            return
        self.reload()
        self.statusBar().showMessage("Устанавливаю ключ…")

        def run() -> None:
            try:
                enroll(server, box[0], self.ctx.get_key())
            finally:
                box[0] = ""

        task = Task(run)
        task.signals.done.connect(lambda _: self._enrolled(server))
        task.signals.failed.connect(self._failed)
        self._keep(task)

    def _enrolled(self, server: Server) -> None:
        server.enrolled = True
        self.ctx.servers.update(server)
        self.ctx.log(server, "сервер добавлен, ключ установлен", True)
        self.statusBar().showMessage(f"{server.name}: ключ установлен", 5000)
        self.reload(keep=server.id)
        self.check(server)

    # ---- remove and refresh ----------------------------------------------

    def remove_server(self) -> None:
        server = self.selected()
        if server is None:
            return
        if self.ctx.confirm(
            "Удалить",
            f"Убрать «{server.name}» из списка? Сам сервер и ключ панели на нём "
            "не меняются.",
        ):
            self.ctx.servers.remove(server.id)
            self.ctx.log(server, "сервер убран из списка", True)
            self.reload()

    def refresh_all(self) -> None:
        for server in self.servers:
            if server.enrolled:
                self.check(server)

    def check(self, server: Server) -> None:
        """The short state in the list: reachable, and what needs attention.

        A server already being checked is not asked again: every check is an SSH
        connection, and "refresh" pressed twice must not double them.
        """
        if server.id in self._checking:
            return
        self._checking.add(server.id)
        self.set_state(server, "проверяю…")

        def read():
            with self.ctx.open_connection(server) as conn:
                return collect(conn)

        task = Task(read)

        def ok(stats) -> None:
            self._checking.discard(server.id)
            alerts = evaluate(stats, None)
            level = worst(alerts)
            version = stats.holdfast_version or "holdfast не установлен"
            note = f" · {alerts[0].text}" if alerts else ""
            self.set_state(server, f"{MARK[level]} {version}{note}")

        def failed(message: str) -> None:
            self._checking.discard(server.id)
            self.set_state(server, f"{MARK[FAIL]} нет связи: {message}")

        task.signals.done.connect(ok)
        task.signals.failed.connect(failed)
        self._keep(task)

    def _failed(self, message: str) -> None:
        self.statusBar().clearMessage()
        self.ctx.show_error(message)
