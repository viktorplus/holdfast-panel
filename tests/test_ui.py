import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash
from PySide6.QtWidgets import QApplication
from test_core import MemorySecrets

from holdfast_panel.enroll import enroll
from holdfast_panel.keys import ensure_keypair
from holdfast_panel.ssh import fetch_host_fingerprint
from holdfast_panel.store import Server, ServerStore
from holdfast_panel.ui.main_window import MainWindow, probe

needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs a bash with HOME")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_the_window_lists_every_saved_server(app, tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    store.add(Server(name="web-1", host="10.0.0.1"))
    store.add(Server(name="db-1", host="10.0.0.2", port=2200, user="deploy"))

    window = MainWindow(store, lambda: None)

    assert window.table.rowCount() == 2
    assert window.table.item(1, 0).text() == "db-1"
    assert window.table.item(1, 1).text() == "10.0.0.2:2200"
    assert window.table.item(1, 3).text() == "ключ не установлен"


@needs_bash
def test_probe_says_when_holdfast_is_not_on_the_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sshd = FakeSshd(home)
    key = ensure_keypair(tmp_path / "keys", MemorySecrets())
    server = Server(
        name="t",
        host="127.0.0.1",
        port=sshd.port,
        host_fingerprint=fetch_host_fingerprint("127.0.0.1", sshd.port),
    )
    enroll(server, PASSWORD, key)

    assert probe(server, key) == ""
    sshd.close()
