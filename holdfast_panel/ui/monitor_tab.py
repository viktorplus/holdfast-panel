from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..context import AppContext
from ..monitor import Stats, collect, human_bytes, human_uptime
from .base import ServerTab

AUTO_REFRESH_MS = 10_000


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return table


def _fill(table: QTableWidget, rows: list[list[str]]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(text))


class MonitorTab(ServerTab):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.last_stats: Stats | None = None
        self.summary = QLabel("Выберите сервер слева")
        self.summary.setWordWrap(True)
        self.disks = _table(["Диск", "Занято", "Размер"])
        self.processes = _table(["PID", "Пользователь", "CPU %", "Память %", "Команда"])
        self.containers = _table(["Контейнер", "Образ", "Состояние"])
        self.reload = QPushButton("Обновить")
        self.reload.clicked.connect(self.refresh)
        self.auto = QCheckBox("Обновлять каждые 10 с")
        self.auto.toggled.connect(self._toggle_timer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

        bar = QHBoxLayout()
        bar.addWidget(self.reload)
        bar.addWidget(self.auto)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.summary)
        for title, table in (
            ("Диски", self.disks),
            ("Процессы (по загрузке процессора)", self.processes),
            ("Контейнеры Docker", self.containers),
        ):
            layout.addWidget(QLabel(f"<b>{title}</b>"))
            layout.addWidget(table)

    def _toggle_timer(self, on: bool) -> None:
        if on:
            self.timer.start(AUTO_REFRESH_MS)
        else:
            self.timer.stop()

    def refresh(self) -> None:
        server = self.server
        if server is None or not server.enrolled:
            self.summary.setText(
                "Выберите сервер слева" if server is None else "Ключ ещё не установлен"
            )
            for table in (self.disks, self.processes, self.containers):
                table.setRowCount(0)
            return
        self.summary.setText("Читаю состояние…")

        def read() -> Stats:
            with self.ctx.open_connection(server) as conn:
                return collect(conn)

        self.work(read, self.show_stats, self.show_failure)

    def show_failure(self, message: str) -> None:
        self.summary.setText(f"Нет связи: {message}")

    def show_stats(self, stats: Stats) -> None:
        swap = (
            f", подкачка {human_bytes(stats.swap_total - stats.swap_free)} "
            f"из {human_bytes(stats.swap_total)}"
            if stats.swap_total
            else ""
        )
        self.summary.setText(
            f"<b>{stats.os_name}</b>, ядро {stats.kernel}<br>"
            f"Работает: {human_uptime(stats.uptime_seconds)} · "
            f"Нагрузка: {stats.load[0]:.2f} / {stats.load[1]:.2f} / {stats.load[2]:.2f} "
            f"на {stats.cpus} ядер · CPU сейчас: {stats.cpu_percent:.0f}%<br>"
            f"Память: занято {human_bytes(stats.mem_total - stats.mem_available)} "
            f"из {human_bytes(stats.mem_total)} ({stats.mem_percent:.0f}%){swap}<br>"
            f"holdfast: {stats.holdfast_version or 'не установлен'}"
        )
        _fill(
            self.disks,
            [[d.mount, f"{d.percent:.0f}%", human_bytes(d.total)] for d in stats.disks],
        )
        _fill(
            self.processes,
            [
                [str(p.pid), p.user, f"{p.cpu:.1f}", f"{p.mem:.1f}", p.command]
                for p in stats.processes
            ],
        )
        _fill(self.containers, [[c.name, c.image, c.status] for c in stats.containers])
        self.last_stats = stats
