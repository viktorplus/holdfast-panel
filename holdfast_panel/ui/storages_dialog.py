from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from ..context import AppContext
from ..store import Storage, StoreError

KIND_LABELS = {
    "pc": "Папка на этом компьютере",
    "remote": "Удалённое хранилище (rclone)",
}


class StoragesDialog(QDialog):
    """The places snapshots can be kept besides the server: a folder here, or
    an rclone remote such as `shared:backups`."""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Хранилища копий")
        self.resize(560, 420)
        self.listing = QListWidget()

        self.name = QLineEdit()
        self.name.setPlaceholderText("например, Мой ноутбук")
        self.kind = QComboBox()
        for key, label in KIND_LABELS.items():
            self.kind.addItem(label, key)
        self.location = QLineEdit()
        self.location.setPlaceholderText("D:\\backups   или   shared:backups")
        browse = QPushButton("Выбрать папку…")
        browse.clicked.connect(self._browse)
        add = QPushButton("Добавить")
        add.clicked.connect(self.add_storage)
        remove = QPushButton("Удалить выбранное")
        remove.clicked.connect(self.remove_storage)
        self.message = QLabel("")
        self.message.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Название", self.name)
        form.addRow("Тип", self.kind)
        row = QHBoxLayout()
        row.addWidget(self.location)
        row.addWidget(browse)
        form.addRow("Где", row)

        hint = QLabel(
            "Удалённое хранилище — это rclone-путь вида <b>имя:папка</b>. "
            "Оно должно быть настроено командой <code>rclone config</code> "
            "на сервере, с которого копия отправляется."
        )
        hint.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Закрыть")

        layout = QVBoxLayout(self)
        layout.addWidget(self.listing)
        layout.addWidget(remove)
        layout.addLayout(form)
        layout.addWidget(add)
        layout.addWidget(hint)
        layout.addWidget(self.message)
        layout.addWidget(buttons)
        self.reload()

    def reload(self) -> None:
        self.storages = self.ctx.storages.load()
        self.listing.clear()
        for storage in self.storages:
            self.listing.addItem(
                f"{storage.name} — {KIND_LABELS[storage.kind]}: {storage.location}"
            )

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Папка для копий")
        if folder:
            self.location.setText(folder)

    def add_storage(self) -> None:
        storage = Storage(
            name=self.name.text().strip(),
            kind=self.kind.currentData(),
            location=self.location.text().strip(),
        )
        try:
            self.ctx.storages.add(storage)
        except StoreError as exc:
            self.message.setText(str(exc))
            return
        self.message.setText("")
        self.name.clear()
        self.location.clear()
        self.reload()

    def remove_storage(self) -> None:
        row = self.listing.currentRow()
        if 0 <= row < len(self.storages):
            self.ctx.storages.remove(self.storages[row].id)
            self.reload()
