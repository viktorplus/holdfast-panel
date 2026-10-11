from __future__ import annotations

import tomllib

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import backups as bk
from .. import config_edit as ce
from ..context import AppContext
from ..restore_flow import Outcome
from .base import OperationTab

HELP = (
    "<b>auto</b> — holdfast сам смотрит в Docker и решает, что брать. "
    "<b>manual</b> — только то, что записано в списке компонентов.<br>"
    "Исключения: <code>volume:имя</code>, <code>path:/путь</code>, "
    "<code>database:контейнер/база</code>, <code>container:имя</code>."
)


class ContentsTab(OperationTab):
    """What goes into the archive: the mode, the exclusions, and the file itself."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.original = ""
        self.shown_id: str | None = None
        self.drafts: dict[str, tuple[str, str]] = {}
        self.mode = QComboBox()
        self.mode.addItems(["auto", "manual"])
        self.excludes = QListWidget()
        self.new_exclude = QLineEdit()
        self.new_exclude.setPlaceholderText("например, volume:myapp_cache")
        add = QPushButton("Добавить исключение")
        add.clicked.connect(self.add_exclude)
        remove = QPushButton("Убрать выбранное")
        remove.clicked.connect(self.remove_exclude)
        apply_button = QPushButton("Применить к тексту ниже")
        apply_button.clicked.connect(self.apply_form)
        self.editor = QPlainTextEdit()
        self.editor.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.message = QLabel("")
        self.message.setWordWrap(True)

        self.load_button = QPushButton("Загрузить с сервера")
        self.load_button.clicked.connect(self.reload_from_server)
        self.diff_button = QPushButton("Показать изменения")
        self.diff_button.clicked.connect(self.show_diff)
        self.save_button = QPushButton("Сохранить на сервер")
        self.save_button.clicked.connect(self.save)
        self.plan_button = QPushButton("Что попадёт в копию")
        self.plan_button.clicked.connect(self.show_plan)
        self.action_buttons = [
            self.load_button,
            self.diff_button,
            self.save_button,
            self.plan_button,
            add,
            remove,
            apply_button,
        ]

        help_label = QLabel(HELP)
        help_label.setWordWrap(True)
        form = QVBoxLayout()
        form.addWidget(help_label)
        row = QHBoxLayout()
        row.addWidget(QLabel("Режим:"))
        row.addWidget(self.mode)
        row.addStretch()
        form.addLayout(row)
        form.addWidget(QLabel("Исключения:"))
        form.addWidget(self.excludes)
        entry = QHBoxLayout()
        entry.addWidget(self.new_exclude, 1)
        entry.addWidget(add)
        entry.addWidget(remove)
        form.addLayout(entry)
        form.addWidget(apply_button)
        top = QWidget()
        top.setLayout(form)

        bottom_bar = QHBoxLayout()
        for button in (
            self.load_button,
            self.diff_button,
            self.save_button,
            self.plan_button,
        ):
            bottom_bar.addWidget(button)
        bottom = QWidget()
        lower = QVBoxLayout(bottom)
        lower.addWidget(
            QLabel("<b>holdfast.toml</b> (можно править текст прямо здесь)")
        )
        lower.addWidget(self.editor)
        lower.addLayout(bottom_bar)
        lower.addWidget(self.message)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(bottom)
        splitter.addWidget(self.log_view)
        splitter.setSizes([260, 300, 140])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

    # ---- loading -----------------------------------------------------------

    def dirty(self) -> bool:
        return self.editor.toPlainText() != self.original

    def refresh(self) -> None:
        """Show the server's file. Unsaved edits are never thrown away silently:
        on the same server they are kept, and when another server is picked they
        are set aside and come back when this server is shown again."""
        server = self.server
        self.refresh_enabled()
        if server is None or not server.enrolled:
            self.message.setText(
                "Выберите сервер слева" if server is None else "Ключ ещё не установлен"
            )
            self.editor.setPlainText("")
            self.excludes.clear()
            self.original, self.shown_id = "", None
            return
        if self.shown_id == server.id and self.dirty():
            self.message.setText(
                "Есть несохранённые правки; «Загрузить с сервера» отменит их."
            )
            return
        if self.shown_id is not None and self.shown_id != server.id and self.dirty():
            self.drafts[self.shown_id] = (self.original, self.editor.toPlainText())
        self.shown_id = server.id
        self.message.setText("Читаю файл…")

        def read() -> str:
            with self.ctx.open_connection(server) as conn:
                return ce.read_config(conn)

        self.work(read, self.show_config, self.show_failure)

    def reload_from_server(self) -> None:
        """The button: forget unsaved edits and read the file again."""
        if self.server is not None:
            self.drafts.pop(self.server.id, None)
        self.original = self.editor.toPlainText()  # nothing counts as unsaved now
        self.refresh()

    def show_failure(self, message: str) -> None:
        self.message.setText(f"Не удалось прочитать конфигурацию: {message}")

    def show_config(self, text: str) -> None:
        self.original = text
        draft = self.drafts.pop(self.server.id, None) if self.server else None
        if draft and draft[0] == text:
            self.editor.setPlainText(draft[1])
            self.message.setText("Восстановлены ваши несохранённые правки.")
            self.load_form(draft[1])
            return
        self.editor.setPlainText(text)
        self.message.setText(
            "Файл на сервере изменился, ваши прежние правки не применены."
            if draft
            else "Файл загружен."
        )
        self.load_form(text)

    def load_form(self, text: str) -> None:
        try:
            backup = tomllib.loads(text).get("backup", {})
        except tomllib.TOMLDecodeError as exc:
            self.message.setText(f"В файле ошибка: {exc}")
            return
        self.mode.setCurrentText(backup.get("mode", "auto") or "auto")
        self.excludes.clear()
        self.excludes.addItems([str(item) for item in backup.get("exclude", [])])

    # ---- the form ------------------------------------------------------------

    def add_exclude(self) -> None:
        item = self.new_exclude.text().strip()
        problem = ce.exclude_problem(item)
        if problem:
            self.message.setText(problem)
            return
        self.excludes.addItem(item)
        self.new_exclude.clear()
        self.message.setText("")

    def remove_exclude(self) -> None:
        row = self.excludes.currentRow()
        if row >= 0:
            self.excludes.takeItem(row)

    def form_excludes(self) -> list[str]:
        return [self.excludes.item(i).text() for i in range(self.excludes.count())]

    def apply_form(self) -> None:
        """Put the form's mode and exclusions into the text, nothing else."""
        try:
            text = ce.set_backup_mode(
                self.editor.toPlainText(), self.mode.currentText()
            )
            text = ce.set_excludes(text, self.form_excludes())
        except ce.ConfigEditError as exc:
            self.message.setText(str(exc))
            return
        self.editor.setPlainText(text)
        self.message.setText("Применено к тексту. Проверьте изменения и сохраните.")

    # ---- diff, save, plan ----------------------------------------------------

    def show_diff(self) -> None:
        text = ce.diff(self.original, self.editor.toPlainText())
        self.log_view.add("\n=== изменения ===\n" + (text or "изменений нет") + "\n")

    def save(self) -> None:
        server = self.server
        new_text = self.editor.toPlainText()
        if server is None or new_text == self.original:
            self.message.setText("Изменений нет.")
            return
        found = ce.problems(new_text)
        if found:
            self.message.setText("Не сохранено: " + "; ".join(found))
            return
        question = ce.diff(self.original, new_text) + "\n\nСохранить на сервере?"
        if not self.ctx.confirm("Сохранить holdfast.toml", question):
            return
        expected = self.original  # the file as it was when this edit started

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                on_stage("Сохраняю…")
                result = ce.save_config(
                    conn, new_text, expected=expected, on_text=on_text
                )
                if not result.ok:
                    on_text(result.dry_run + "\n")
                    return Outcome(False, "rollback", result.message)
                return Outcome(
                    True, "saved", f"{result.message}; копия: {result.backup_path}"
                )

        def after(result: Outcome) -> None:
            if result.ok:
                self.original = new_text
            self.message.setText(result.message)

        self.operate("правка holdfast.toml", run, after=after)

    def show_plan(self) -> None:
        server = self.server
        if server is None:
            return

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                on_stage("Состав копии…")
                on_text(bk.dry_run(conn))
                return Outcome(True, "plan", "состав показан")

        self.operate("что попадёт в копию", run)
