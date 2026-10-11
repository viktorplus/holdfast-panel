from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)


class AddServerDialog(QDialog):
    """Name, address, login and the password used once to install the key."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить сервер")
        self.name = QLineEdit()
        self.name.setPlaceholderText("например, web-1")
        self.host = QLineEdit()
        self.host.setPlaceholderText("адрес или IP")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QLineEdit("root")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("нужен один раз, не сохраняется")

        form = QFormLayout()
        form.addRow("Имя", self.name)
        form.addRow("Адрес", self.host)
        form.addRow("SSH-порт", self.port)
        form.addRow("Пользователь", self.user)
        form.addRow("Пароль", self.password)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Подключить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if all(w.text().strip() for w in (self.name, self.host, self.user)) and (
            self.password.text()
        ):
            self.accept()

    def values(self) -> tuple[str, str, int, str, str]:
        return (
            self.name.text().strip(),
            self.host.text().strip(),
            self.port.value(),
            self.user.text().strip(),
            self.password.text(),
        )
