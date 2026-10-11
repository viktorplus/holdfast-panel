"""Helpers for tests that open real Qt widgets (offscreen) without a server."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtWidgets import QApplication

from holdfast_panel.context import AppContext
from holdfast_panel.library import Library
from holdfast_panel.ssh import Result
from holdfast_panel.store import Server, ServerStore, StorageStore

FIXTURES = Path(__file__).parent / "fixtures"
COLLECTED = (FIXTURES / "collected_debian_wsl.txt").read_text(encoding="utf-8")


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


class FakeConn:
    """Answers commands from a table of (substring, Result) pairs."""

    def __init__(self, answers=None, log=None):
        self.answers = answers or {}
        self.log = log if log is not None else []

    def run(self, command, timeout=60):
        self.log.append(command)
        for needle, result in self.answers.items():
            if needle in command:
                return result
        return Result(0, "", "")

    def run_stream(self, command, on_text, timeout=0):
        result = self.run(command, timeout)
        if result.out:
            on_text(result.out)
        return result.code

    def put_bytes(self, path, data, mode="600"):
        self.log.append(f"put {path}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def make_ctx(tmp_path, conn_factory=None, servers=(), **extra) -> AppContext:
    ctx = AppContext(
        data_dir=tmp_path,
        servers=ServerStore(tmp_path / "servers.json"),
        storages=StorageStore(tmp_path / "storages.json"),
        library=Library(tmp_path / "templates"),
        get_key=lambda: None,
        open_connection=conn_factory
        or (lambda server: FakeConn({"##end": Result(0, COLLECTED, "")})),
        show_error=lambda message: ctx.errors.append(message),
        confirm=lambda title, text: ctx.confirmed.append((title, text)) or True,
        **extra,
    )
    ctx.errors = []
    ctx.confirmed = []
    for server in servers:
        ctx.servers.add(server)
    return ctx


def enrolled(name="web-1", host="10.0.0.1") -> Server:
    return Server(name=name, host=host, enrolled=True, host_fingerprint="SHA256:x")


def settle(ctx: AppContext, rounds: int = 3) -> None:
    """Let background tasks finish and deliver their results to the GUI thread."""
    app = application()
    for _ in range(rounds):
        ctx.pool.waitForDone(5000)
        app.processEvents()
