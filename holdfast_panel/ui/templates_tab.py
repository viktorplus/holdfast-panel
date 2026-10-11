from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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

from ..context import AppContext
from ..group import run_on_servers
from ..restore_flow import Outcome
from ..templates import (
    Template,
    TemplateError,
    check_values,
    preview,
    run_template,
)
from .base import OperationTab

SKELETON = {
    "id": "my-template",
    "name": "Мой шаблон",
    "description": "Что делает этот шаблон",
    "variables": [{"name": "name", "label": "Имя", "default": "world"}],
    "steps": [{"title": "Приветствие", "run": "echo hello {{name}}"}],
}


class TemplateEditor(QDialog):
    """A template as JSON. It is saved only if it parses."""

    def __init__(self, ctx: AppContext, text: str, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Редактор шаблона (JSON)")
        self.resize(720, 560)
        self.editor = QPlainTextEdit(text)
        self.editor.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.message = QLabel(
            "Переменные подставляются как {{имя}} и всегда экранируются; "
            "тип password маскируется в выводе."
        )
        self.message.setWordWrap(True)
        self.saved: Template | None = None
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.editor)
        layout.addWidget(self.message)
        layout.addWidget(buttons)

    def save(self) -> None:
        try:
            self.saved = self.ctx.library.import_text(self.editor.toPlainText())
        except TemplateError as exc:
            self.message.setText(f"Не сохранено: {exc}")
            return
        self.accept()


class TemplatesTab(OperationTab):
    """Ready-made and custom step lists, run on this server or on all of them."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.templates: list[Template] = []
        self.listing = QListWidget()
        self.listing.currentRowChanged.connect(self._selected)
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.form = QFormLayout()
        self.fields: dict[str, QLineEdit] = {}
        self.message = QLabel("")
        self.message.setWordWrap(True)

        self.preview_button = QPushButton("Показать команды")
        self.preview_button.clicked.connect(self.show_preview)
        self.run_button = QPushButton("Запустить на этом сервере")
        self.run_button.clicked.connect(self.run_here)
        self.group_button = QPushButton("Запустить на всех серверах")
        self.group_button.clicked.connect(self.run_everywhere)
        self.action_buttons = [self.run_button, self.group_button]

        new = QPushButton("Новый")
        new.clicked.connect(self.new_template)
        edit = QPushButton("Править")
        edit.clicked.connect(self.edit_template)
        copy = QPushButton("Копия")
        copy.clicked.connect(self.copy_template)
        delete = QPushButton("Удалить")
        delete.clicked.connect(self.delete_template)
        paste = QPushButton("Импорт…")
        paste.clicked.connect(self.import_template)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.listing)
        row = QHBoxLayout()
        for button in (new, edit, copy, delete, paste):
            row.addWidget(button)
        left_layout.addLayout(row)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.description)
        right_layout.addLayout(self.form)
        buttons = QHBoxLayout()
        for button in (self.preview_button, self.run_button, self.group_button):
            buttons.addWidget(button)
        right_layout.addLayout(buttons)
        right_layout.addWidget(self.message)
        right_layout.addWidget(self.stage_label)
        right_layout.addStretch()

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(left)
        top.addWidget(right)
        top.setSizes([360, 520])
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top)
        splitter.addWidget(self.log_view)
        splitter.setSizes([360, 260])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)
        self.reload()

    # ---- the library ------------------------------------------------------------

    def reload(self, keep: str | None = None) -> None:
        self.templates = self.ctx.library.all()
        self.listing.blockSignals(True)
        self.listing.clear()
        for template in self.templates:
            mark = "" if template.builtin else " ✎"
            self.listing.addItem(f"{template.name}{mark}")
        self.listing.blockSignals(False)
        index = next((i for i, t in enumerate(self.templates) if t.id == keep), 0)
        self.listing.setCurrentRow(index)
        self._selected(index)
        problems = self.ctx.library.problems()
        if problems:
            self.message.setText("Не прочитаны файлы шаблонов: " + "; ".join(problems))

    def refresh(self) -> None:
        self.refresh_enabled()

    def refresh_enabled(self) -> None:
        # running on every server needs no selected server, only that nothing runs
        self.run_button.setEnabled(
            not self.busy and bool(self.server and self.server.enrolled)
        )
        self.group_button.setEnabled(not self.busy)

    def current(self) -> Template | None:
        row = self.listing.currentRow()
        return self.templates[row] if 0 <= row < len(self.templates) else None

    def _selected(self, _row: int) -> None:
        template = self.current()
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = {}
        if template is None:
            self.description.setText("")
            return
        danger = (
            "  <b style='color:#c62828'>⚠ содержит опасные шаги</b>"
            if template.dangerous
            else ""
        )
        self.description.setText(
            f"<b>{template.name}</b>{danger}<br>{template.description}"
        )
        for var in template.variables:
            edit = QLineEdit(var.default)
            if var.kind == "password":
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[var.name] = edit
            self.form.addRow(
                var.label + ("" if var.required else " (необязательно)"), edit
            )

    def clear_passwords(self) -> None:
        """The values of a run are already in hand; do not leave passwords in the form."""
        template = self.current()
        for var in template.variables if template else ():
            if var.kind == "password" and var.name in self.fields:
                self.fields[var.name].clear()

    def values(self) -> dict[str, str]:
        return {name: edit.text() for name, edit in self.fields.items()}

    def new_template(self) -> None:
        self._edit(json.dumps(SKELETON, indent=2, ensure_ascii=False))

    def import_template(self) -> None:
        self._edit("")

    def edit_template(self) -> None:
        template = self.current()
        if template is None:
            return
        if template.builtin:
            self.message.setText(
                "Встроенный шаблон не правится: сделайте копию и правьте её."
            )
            return
        self._edit(template.to_json())

    def copy_template(self) -> None:
        template = self.current()
        if template is None:
            return
        taken = {t.id for t in self.templates}
        new_id = f"{template.id}-copy"
        number = 2
        while new_id in taken:
            new_id, number = f"{template.id}-copy{number}", number + 1
        data = template.to_dict()
        data["id"], data["name"] = new_id, f"{template.name} (копия)"
        try:
            self.ctx.library.import_text(json.dumps(data))
        except TemplateError as exc:
            self.message.setText(str(exc))
            return
        self.reload(keep=new_id)

    def delete_template(self) -> None:
        template = self.current()
        if template is None:
            return
        if template.builtin:
            self.message.setText("Встроенный шаблон удалить нельзя.")
            return
        if self.ctx.confirm("Удалить шаблон", f"Удалить «{template.name}»?"):
            self.ctx.library.delete(template.id)
            self.reload()

    def _edit(self, text: str) -> None:
        dialog = TemplateEditor(self.ctx, text, self.ctx.parent_window)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved:
            self.reload(keep=dialog.saved.id)

    # ---- previewing and running ---------------------------------------------------

    def _checked(self) -> tuple[Template, dict[str, str]] | None:
        template = self.current()
        if template is None:
            return None
        try:
            return template, check_values(template, self.values())
        except TemplateError as exc:
            self.message.setText(str(exc))
            return None

    def _preview_text(self, template: Template, values: dict[str, str]) -> str:
        lines = []
        danger = {s.title for s in template.steps if s.dangerous}
        for number, (title, shown) in enumerate(preview(template, values), 1):
            flag = "  ⚠ ОПАСНО" if title in danger else ""
            lines.append(f"{number}. {title}{flag}\n{shown}\n")
        return "\n".join(lines)

    def show_preview(self) -> None:
        checked = self._checked()
        if checked:
            template, values = checked
            self.message.setText("")
            self.log_view.add(
                f"\n=== команды шаблона «{template.name}» ===\n"
                + self._preview_text(template, values)
            )

    def run_here(self) -> None:
        checked = self._checked()
        server = self.server
        if checked is None or server is None or not server.enrolled:
            return
        template, values = checked
        self.message.setText("")
        question = (
            f"Запустить «{template.name}» на {server.name}?\n\n"
            + self._preview_text(template, values)
        )
        if not self.ctx.confirm("Запуск шаблона", question):
            return
        self.clear_passwords()

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:

                def event(kind: str, text: str) -> None:
                    if kind == "step":
                        on_stage(text)
                        on_text(f"\n▶ {text}\n")
                    elif kind == "output":
                        on_text(text)
                    else:
                        on_text(f"{text}\n")

                result = run_template(conn, template, values, event)
            message = (
                "выполнено"
                if result.ok
                else f"остановилось на шаге «{result.failed_step}»"
            )
            return Outcome(result.ok, "template", message)

        self.operate(f"шаблон {template.id}", run)

    def run_everywhere(self) -> None:
        checked = self._checked()
        if checked is None:
            return
        template, values = checked
        servers = [s for s in self.ctx.servers.load() if s.enrolled]
        if not servers:
            self.message.setText("Нет серверов с установленным ключом.")
            return
        names = ", ".join(s.name for s in servers)
        question = (
            f"Запустить «{template.name}» по очереди на серверах: {names}?\n"
            "Сбой на одном сервере не останавливает остальные.\n\n"
            + self._preview_text(template, values)
        )
        if not self.ctx.confirm("Запуск на всех серверах", question):
            return
        self.clear_passwords()

        def run(on_text, on_stage):
            def event(server: str, kind: str, text: str) -> None:
                if kind == "server":
                    on_stage(f"Сервер {server}")
                    on_text(f"\n######## {server} ########\n")
                elif kind == "step":
                    on_text(f"\n▶ {text}\n")
                else:
                    on_text(text if kind == "output" else f"{text}\n")

            results = run_on_servers(
                servers,
                template,
                values,
                self.ctx.open_connection,
                self.ctx.log_path,
                event,
            )
            failed = [r.server for r in results if not r.ok]
            summary = "; ".join(f"{r.server}: {r.detail}" for r in results)
            on_text(f"\nИтог: {summary}\n")
            return Outcome(
                not failed,
                "group",
                f"сбой на: {', '.join(failed)}" if failed else "везде выполнено",
            )

        self.operate(f"шаблон {template.id} на всех серверах", run, log=False)
