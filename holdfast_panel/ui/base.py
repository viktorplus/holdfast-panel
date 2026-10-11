"""Shared bits of the tabs: a tab that belongs to one server, and a log view."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from ..context import AppContext
from ..store import Server
from .workers import Task


class LogView(QPlainTextEdit):
    """Output of a running command; read-only, follows the end."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setMaximumBlockCount(5000)

    def add(self, text: str) -> None:
        self.moveCursor(self.textCursor().MoveOperation.End)
        self.insertPlainText(text)
        self.ensureCursorVisible()


class ServerTab(QWidget):
    """A tab showing one server at a time.

    The window sets `server` on every tab and calls `refresh` only on the
    visible one, so a tab that is not looked at does no work.
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.server: Server | None = None
        self._tasks: list[Task] = []  # kept: a Task must outlive its signals

    def refresh(self) -> None:
        """Reload what this tab shows for the current server."""

    def work(
        self,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[str], None] | None = None,
    ) -> None:
        """Run `fn` off the GUI thread; `done`/`failed` run back on it."""
        server = self.server
        task = Task(fn)
        self._tasks.append(task)

        def finish(result: Any) -> None:
            self._tasks.remove(task)
            if self.server is server:  # the user may have picked another server
                done(result)

        def fail(message: str) -> None:
            self._tasks.remove(task)
            if self.server is server:
                (failed or self.ctx.show_error)(message)

        task.signals.done.connect(finish)
        task.signals.failed.connect(fail)
        self.ctx.pool.start(task)
