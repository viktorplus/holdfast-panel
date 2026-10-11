from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..context import AppContext
from ..diagnostics import FAIL, OK, WARN, Diagnosis, diagnose
from .base import ServerTab
from .monitor_tab import _fill, _table

WORDS = {
    OK: "● норма",
    WARN: "▲ внимание",
    FAIL: "✖ проблема",
    "unknown": "? неизвестно",
}
COLORS = {OK: "#2e7d32", WARN: "#b26a00", FAIL: "#c62828", "unknown": "#666666"}


class DiagnosticsTab(ServerTab):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.diagnosis: Diagnosis | None = None
        self.title = QLabel("Диагностика не запускалась")
        self.items = _table(["Проверка", "Статус", "Подробности"])
        self.audit = _table(
            ["Группа", "Проверка безопасности", "Статус", "Подробности"]
        )
        self.run_button = QPushButton("Запустить диагностику")
        self.run_button.clicked.connect(self.run_diagnostics)

        bar = QHBoxLayout()
        bar.addWidget(self.run_button)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.title)
        layout.addWidget(self.items)
        layout.addWidget(QLabel("<b>Аудит безопасности: что требует внимания</b>"))
        layout.addWidget(self.audit)

    def refresh(self) -> None:
        # Diagnostics runs the audit, which takes a while: only on request.
        self.diagnosis = None
        self.items.setRowCount(0)
        self.audit.setRowCount(0)
        server = self.server
        self.run_button.setEnabled(server is not None and server.enrolled)
        if server is None:
            self.title.setText("Выберите сервер слева")
        elif not server.enrolled:
            self.title.setText("Ключ ещё не установлен")
        else:
            self.title.setText("Диагностика не запускалась")

    def run_diagnostics(self) -> None:
        server = self.server
        if server is None or not server.enrolled:
            return
        self.run_button.setEnabled(False)
        self.title.setText("Проверяю… (аудит может занять до минуты)")

        def run() -> Diagnosis:
            with self.ctx.open_connection(server) as conn:
                return diagnose(conn, server.backup_root)

        def failed(message: str) -> None:
            self.run_button.setEnabled(True)
            self.title.setText(f"Не удалось проверить: {message}")

        self.work(run, self.show, failed)

    def show(self, diagnosis: Diagnosis) -> None:
        self.run_button.setEnabled(True)
        self.diagnosis = diagnosis
        self.title.setText(f"Итог: {WORDS[diagnosis.worst]}")
        _fill(
            self.items,
            [[i.name, WORDS[i.status], i.detail] for i in diagnosis.items],
        )
        for row, item in enumerate(diagnosis.items):
            self.items.item(row, 1).setForeground(QColor(COLORS[item.status]))
        notable = [f for f in diagnosis.audit if f.status in ("FAIL", "WARN")]
        notable.sort(key=lambda f: f.status != "FAIL")
        _fill(
            self.audit,
            [[f.group, f.title, f.status, f.detail] for f in notable],
        )
