"""Shared bits of the tabs: a tab that belongs to one server, and a log view."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QWidget

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
        self._latest: dict[str, object] = {}

    def refresh(self) -> None:
        """Reload what this tab shows for the current server."""

    def work(
        self,
        fn: Callable[[], Any],
        done: Callable[[Any], None],
        failed: Callable[[str], None] | None = None,
        key: str = "refresh",
    ) -> None:
        """Run `fn` off the GUI thread; `done`/`failed` run back on it.

        Only the newest call for a `key` is heard: if the user switches the
        view while an older request is still on its way, the older answer is
        dropped instead of overwriting the newer one.
        """
        server = self.server
        token = object()
        self._latest[key] = token
        task = Task(fn)
        self._tasks.append(task)

        def current() -> bool:
            # the user may have picked another server, or asked again since
            return self.server is server and self._latest.get(key) is token

        def finish(result: Any) -> None:
            self._tasks.remove(task)
            if current():
                done(result)

        def fail(message: str) -> None:
            self._tasks.remove(task)
            if current():
                (failed or self.ctx.show_error)(message)

        task.signals.done.connect(finish)
        task.signals.failed.connect(fail)
        self.ctx.pool.start(task)


class _OperationSignals(QObject):
    text = Signal(str)
    stage = Signal(str)
    question = Signal(str, str, object)


def summarize(result: Any) -> tuple[bool, str]:
    """(worked, one line) for whatever an operation returned."""
    if hasattr(result, "ok"):
        return bool(result.ok), str(getattr(result, "message", "") or "")
    if isinstance(result, int):
        return result == 0, f"код {result}"
    return True, ""


class OperationTab(ServerTab):
    """A tab that runs long operations and shows their output as it comes.

    `operate` runs `fn(on_text, on_stage)` off the GUI thread. Its output goes
    to `log_view`, the stage to `stage_label`, the buttons in `action_buttons`
    are disabled meanwhile, and the result is written to the action log (unless
    the operation logs itself, as a run on several servers does).
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.log_view = LogView()
        self.stage_label = QLabel("")
        self.action_buttons: list = []
        self._signals = _OperationSignals()
        self._signals.text.connect(self.log_view.add)
        self._signals.stage.connect(self.stage_label.setText)
        self._signals.question.connect(self._answer_question)
        self.busy = False

    def _answer_question(self, title: str, text: str, reply: object) -> None:
        """Runs on the GUI thread: the one place a dialog may be opened."""
        event, box = reply
        box.append(bool(self.ctx.confirm(title, text)))
        event.set()

    def ask(self, title: str, text: str) -> bool:
        """Ask the user from inside an operation (a worker thread) and wait.

        Qt allows dialogs only on the GUI thread, so the question is sent there
        by a signal and this thread blocks until it is answered.
        """
        event, box = threading.Event(), []
        self._signals.question.emit(title, text, (event, box))
        event.wait()
        return box[0]

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for button in self.action_buttons:
            button.setEnabled(not busy)

    def operate(
        self,
        label: str,
        fn: Callable[[Callable[[str], None], Callable[[str], None]], Any],
        after: Callable[[Any], None] | None = None,
        log: bool = True,
    ) -> None:
        server = self.server
        name = server.name if server else "-"
        self.set_busy(True)
        self.log_view.add(f"\n=== {label} ===\n")
        task = Task(lambda: fn(self._signals.text.emit, self._signals.stage.emit))
        self._tasks.append(task)

        def finish(result: Any) -> None:
            self._tasks.remove(task)
            self.set_busy(False)
            ok, detail = summarize(result)
            self.stage_label.setText(
                ("Готово: " if ok else "Не удалось: ") + (detail or label)
            )
            self.log_view.add(f"\n--- {'готово' if ok else 'не удалось'}: {detail}\n")
            if log:
                self.ctx.log(name, label, ok, detail)
            if after:
                after(result)

        def fail(message: str) -> None:
            self._tasks.remove(task)
            self.set_busy(False)
            self.stage_label.setText(f"Ошибка: {message}")
            self.log_view.add(f"\n--- ошибка: {message}\n")
            if log:
                self.ctx.log(name, label, False, message)

        task.signals.done.connect(finish)
        task.signals.failed.connect(fail)
        self.ctx.pool.start(task)
