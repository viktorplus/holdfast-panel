from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import backups as bk
from ..backups import BackupError, Listing, SnapshotInfo
from ..context import AppContext
from ..monitor import human_bytes
from ..restore_flow import (
    Outcome,
    recover_new_server,
    restore_snapshot,
    verify_snapshot,
)
from ..ssh import Connection
from .base import OperationTab
from .monitor_tab import _fill, _table
from .storages_dialog import StoragesDialog

HEADERS = ["Дата", "Машина", "Размер", "Состав", "Шифрование"]
SERVER = "server"


def host_label_of(
    conn: Connection, config_path: str = "/etc/holdfast/holdfast.toml"
) -> str:
    """The machine's own name, as holdfast knows it ('' when it cannot be read).

    The file is parsed as TOML rather than searched with grep: a comment after
    the value, single quotes, or another key that starts the same way would each
    give a wrong name, and a wrong name switches off holdfast's check that a
    snapshot belongs to this machine.
    """
    result = conn.run(f"cat {shlex.quote(config_path)} 2>/dev/null")
    try:
        return str(tomllib.loads(result.out).get("host_label", "") or "")
    except tomllib.TOMLDecodeError:
        return ""


def other_machine_note(snapshot_label: str, this_label: str) -> str:
    """Said in the confirmation whenever the wrong-machine guard is switched off."""
    return (
        f"⚠ Снимок снят на другой машине ({snapshot_label}), а эта называется "
        f"{this_label}. Подтверждая, вы разрешаете восстановить чужой снимок "
        "(--from-other-host).\n\n"
    )


class BackupsTab(OperationTab):
    """Snapshots in three places: on the server, on this computer, on a remote."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.listing = Listing()
        self.label = ""
        self.where = QComboBox()
        self.where.currentIndexChanged.connect(self._where_changed)
        self.table = _table(HEADERS)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(self.table.SelectionMode.SingleSelection)
        self.note = QLabel("")
        self.note.setWordWrap(True)

        self.create_button = QPushButton("Создать бэкап")
        self.create_button.clicked.connect(self.create_backup)
        self.verify_button = QPushButton("Проверить копию")
        self.verify_button.clicked.connect(self.verify)
        self.restore_button = QPushButton("Восстановить…")
        self.restore_button.clicked.connect(self.restore)
        self.new_button = QPushButton("Поднять на новом сервере…")
        self.new_button.clicked.connect(self.recover)
        self.copy_button = QPushButton("Скопировать в…")
        self.copy_button.clicked.connect(self.copy_to)
        self.storages_button = QPushButton("Хранилища…")
        self.storages_button.clicked.connect(self.manage_storages)
        self.action_buttons = [
            self.create_button,
            self.verify_button,
            self.restore_button,
            self.new_button,
            self.copy_button,
        ]

        top = QHBoxLayout()
        top.addWidget(QLabel("Где смотреть:"))
        top.addWidget(self.where, 1)
        top.addWidget(self.storages_button)
        buttons = QHBoxLayout()
        for button in (
            self.create_button,
            self.verify_button,
            self.restore_button,
            self.copy_button,
            self.new_button,
        ):
            buttons.addWidget(button)
        upper = QVBoxLayout()
        upper.addLayout(top)
        upper.addWidget(self.table)
        upper.addWidget(self.note)
        upper.addLayout(buttons)
        upper.addWidget(self.stage_label)
        head = QWidget()
        head.setLayout(upper)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(head)
        splitter.addWidget(self.log_view)
        splitter.setSizes([420, 200])
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)
        self._fill_where()

    # ---- where to look -----------------------------------------------------

    def _fill_where(self) -> None:
        self.where.blockSignals(True)
        self.where.clear()
        self.where.addItem("На сервере", (SERVER, ""))
        for storage in self.ctx.storages.load():
            prefix = "Этот компьютер" if storage.kind == "pc" else "Удалённо"
            self.where.addItem(
                f"{prefix}: {storage.name}", (storage.kind, storage.location)
            )
        self.where.blockSignals(False)

    def _where_changed(self, _index: int) -> None:
        self.refresh()

    def manage_storages(self) -> None:
        StoragesDialog(self.ctx, self.ctx.parent_window).exec()
        self._fill_where()
        self.refresh()

    def refresh_enabled(self) -> None:
        super().refresh_enabled()
        enabled = not self.busy and bool(self.server and self.server.enrolled)
        for widget in (self.where, self.storages_button):
            widget.setEnabled(enabled)

    def current_place(self) -> tuple[str, str]:
        return self.where.currentData() or (SERVER, "")

    def selected(self) -> SnapshotInfo | None:
        row = self.table.currentRow()
        return (
            self.listing.snapshots[row]
            if 0 <= row < len(self.listing.snapshots)
            else None
        )

    # ---- listing ------------------------------------------------------------

    def refresh(self) -> None:
        self._fill_where_keep()
        server = self.server
        self.listing = Listing()
        self.table.setRowCount(0)
        self.refresh_enabled()
        if server is None or not server.enrolled:
            self.note.setText(
                "Выберите сервер слева" if server is None else "Ключ ещё не установлен"
            )
            return
        kind, location = self.current_place()
        self.note.setText("Читаю список…")

        def read() -> tuple[Listing, str]:
            with self.ctx.open_connection(server) as conn:
                label = host_label_of(conn)
                if kind == SERVER:
                    return bk.list_server(conn, server.backup_root), label
                if kind == "pc":
                    return bk.list_pc(Path(location), label or None), label
                if not label:
                    raise BackupError(
                        "у сервера нет host_label: неизвестно, какую папку смотреть"
                    )
                return bk.list_remote(conn, location, label), label

        self.work(read, self.show_listing, self.show_failure)

    def _fill_where_keep(self) -> None:
        current = self.where.currentIndex()
        self._fill_where()
        if 0 <= current < self.where.count():
            self.where.blockSignals(True)
            self.where.setCurrentIndex(current)
            self.where.blockSignals(False)

    def show_failure(self, message: str) -> None:
        self.note.setText(f"Не удалось получить список: {message}")

    def show_listing(self, result: tuple[Listing, str]) -> None:
        self.listing, self.label = result
        rows = []
        for s in self.listing.snapshots:
            parts = ", ".join(sorted({c.kind or "?" for c in s.components}))
            rows.append(
                [
                    s.created_at.replace("T", " ")[:19] or s.name,
                    s.host_label,
                    human_bytes(s.total_bytes),
                    f"{len(s.components)}: {parts}",
                    "да" if s.encrypted else "нет",
                ]
            )
        _fill(self.table, rows)
        problems = (
            f" Нечитаемых снимков: {len(self.listing.problems)}."
            if self.listing.problems
            else ""
        )
        self.note.setText(
            (f"Снимков: {len(rows)}." if rows else "Снимков здесь нет.")
            + problems
            + (f" Имя машины: {self.label}." if self.label else "")
        )

    # ---- operations ---------------------------------------------------------

    def _need_snapshot(self, only_server: bool = False) -> SnapshotInfo | None:
        info = self.selected()
        if info is None:
            self.ctx.show_error("Выберите снимок в списке.")
            return None
        if only_server and self.current_place()[0] != SERVER:
            self.ctx.show_error("Эта операция работает со снимком на сервере.")
            return None
        return info

    def create_backup(self) -> None:
        server = self.server
        if server is None:
            return

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                on_stage("Состав копии…")
                plan = bk.dry_run(conn)
                on_text(plan + "\n")
                on_stage("Идёт бэкап…")
                return Outcome(bk.run_backup(conn, on_text) == 0, "backup", "бэкап")

        text = "Будет снят бэкап этого сервера. Продолжить?"
        if self.ctx.confirm("Создать бэкап", text):
            self.operate("бэкап", run, after=lambda _: self.refresh())

    def _key(self) -> Path | None:
        key = self.ctx.ask_key_file()
        if key is None:
            self.ctx.show_error(
                "Нужен файл приватного ключа: без него снимок не прочитать."
            )
        return key

    def verify(self) -> None:
        info = self._need_snapshot(only_server=True)
        key = self._key() if info else None
        server = self.server
        if info is None or key is None or server is None:
            return

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                on_stage("Проверка снимка…")
                ok = verify_snapshot(conn, info.location, key, on_text)
                return Outcome(
                    ok, "verify", "снимок читается" if ok else "снимок не читается"
                )

        self.operate(f"проверка {info.name}", run)

    def restore(self) -> None:
        info = self._need_snapshot()
        server = self.server
        if info is None or server is None:
            return
        kind, location = self.current_place()
        key = self._key()
        if key is None:
            return

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                directory = self._bring_to_server(
                    conn, info, kind, location, server.backup_root, on_text, on_stage
                )
                # read here, in the worker, from the server itself: not from state
                # the window left from some earlier listing
                this = host_label_of(conn)
                from_other = bool(this and info.host_label and info.host_label != this)
                note = other_machine_note(info.host_label, this) if from_other else ""
                return restore_snapshot(
                    conn,
                    directory,
                    key,
                    from_other_host=from_other,
                    confirm=lambda plan: self.ask("Восстановление: план", note + plan),
                    on_text=on_text,
                    on_stage=on_stage,
                )

        self.operate(f"восстановление {info.name}", run, after=lambda _: self.refresh())

    def recover(self) -> None:
        info = self._need_snapshot()
        server = self.server
        if info is None or server is None:
            return
        kind, location = self.current_place()
        key = self._key()
        if key is None:
            return

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                directory = self._bring_to_server(
                    conn, info, kind, location, server.backup_root, on_text, on_stage
                )
                this = host_label_of(conn)
                from_other = bool(this and info.host_label and info.host_label != this)
                note = other_machine_note(info.host_label, this) if from_other else ""
                on_stage("Новый сервер из снимка")
                return recover_new_server(
                    conn,
                    directory,
                    info,
                    key,
                    from_other_host=from_other,
                    confirm=lambda text: self.ask(
                        "Новый сервер из снимка", note + text
                    ),
                    on_text=on_text,
                    on_stage=on_stage,
                )

        self.operate(
            f"новый сервер из {info.name}", run, after=lambda _: self.refresh()
        )

    def _bring_to_server(
        self, conn, info, kind, location, root, on_text, on_stage
    ) -> str:
        """The folder on the server holding this snapshot, copying it there first
        if it lives on this computer or on a remote."""
        if kind == SERVER:
            return info.location
        on_stage("Копирую снимок на сервер…")
        if kind == "pc":
            return bk.push_from_pc(conn, Path(info.location), root)
        code = bk.pull_from_remote(
            conn, location, info.host_label, info.name, root, on_text
        )
        if code != 0:
            raise BackupError("снимок не удалось скачать с удалённого хранилища")
        return f"{root.rstrip('/')}/{info.name}"

    def copy_to(self) -> None:
        info = self._need_snapshot(only_server=True)
        server = self.server
        if info is None or server is None:
            return
        targets = [s for s in self.ctx.storages.load()]
        if not targets:
            self.ctx.show_error("Сначала добавьте хранилище кнопкой «Хранилища…».")
            return
        options = [
            f"{'Компьютер' if s.kind == 'pc' else 'Удалённо'}: {s.name}"
            for s in targets
        ]
        index = self.ctx.choose("Куда скопировать", options)
        if index is None:
            return
        target = targets[index]
        label = info.host_label or self.label

        def run(on_text, on_stage):
            with self.ctx.open_connection(server) as conn:
                on_stage(f"Копирую в «{target.name}»…")
                if target.kind == "pc":
                    path = bk.pull_to_pc(
                        conn, info.location, Path(target.location), label
                    )
                    return Outcome(True, "copy", f"скопировано в {path}")
                code = bk.push_to_remote(
                    conn, info.location, target.location, label, on_text
                )
                return Outcome(
                    code == 0,
                    "copy",
                    "отправлено" if code == 0 else f"rclone вернул {code}",
                )

        self.operate(f"копирование {info.name} → {target.name}", run)
