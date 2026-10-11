from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout

from .. import actionlog
from ..context import AppContext
from .base import ServerTab
from .monitor_tab import _fill, _table


class LogTab(ServerTab):
    """What the panel did: for the selected server, or for all of them."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.only_this = QCheckBox("Только выбранный сервер")
        self.only_this.setChecked(True)
        # A bound method, not a lambda capturing self: the lambda would make a
        # reference cycle, the widget would outlive the QApplication, and the
        # process would abort at exit.
        self.only_this.toggled.connect(self.refresh)
        self.table = _table(["Время", "Сервер", "Действие", "Результат", "Подробности"])
        bar = QHBoxLayout()
        bar.addWidget(self.only_this)
        bar.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.table)

    def refresh(self) -> None:
        name = self.server.name if self.server and self.only_this.isChecked() else None
        entries = actionlog.read(self.ctx.log_path, limit=500, server=name)
        _fill(
            self.table,
            [
                [
                    e.at.replace("T", " ")[:19],
                    e.server,
                    e.action,
                    "успешно" if e.ok else "ошибка",
                    e.detail,
                ]
                for e in entries
            ],
        )
