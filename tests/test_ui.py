import pytest
from ui_support import (
    COLLECTED,
    FakeConn,
    application,
    enrolled,
    make_ctx,
    settle,
)

from holdfast_panel.ssh import Result, SshError
from holdfast_panel.store import Server
from holdfast_panel.ui.main_window import MainWindow
from holdfast_panel.ui.monitor_tab import MonitorTab


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


def test_the_window_lists_every_saved_server(tmp_path):
    ctx = make_ctx(
        tmp_path,
        servers=[
            Server(name="web-1", host="10.0.0.1"),
            Server(name="db-1", host="10.0.0.2", port=2200, user="deploy"),
        ],
    )
    window = MainWindow(ctx)

    assert window.table.rowCount() == 2
    assert window.table.item(1, 0).text() == "db-1"
    assert window.table.item(1, 1).text() == "10.0.0.2:2200"
    assert window.table.item(1, 2).text() == "ключ не установлен"


def test_an_enrolled_server_shows_its_state_and_holdfast_version(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    window = MainWindow(ctx)
    settle(ctx)

    state = window.table.item(0, 2).text()
    assert state.startswith("●") and "holdfast не установлен" in state


def test_an_unreachable_server_is_marked_in_the_list(tmp_path):
    def refuse(server):
        raise SshError("timed out")

    ctx = make_ctx(tmp_path, conn_factory=refuse, servers=[enrolled()])
    window = MainWindow(ctx)
    settle(ctx)

    assert window.table.item(0, 2).text() == "✖ нет связи: timed out"


def test_selecting_a_server_fills_the_monitor_with_real_debian_output(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    window = MainWindow(ctx)
    settle(ctx)

    window.table.selectRow(0)
    settle(ctx)

    monitor = window.tab_list[0]
    assert isinstance(monitor, MonitorTab)
    assert "Debian GNU/Linux 13" in monitor.summary.text()
    assert "8 ядер" in monitor.summary.text()
    # the default checks: every list the screen shows renders non-empty
    assert monitor.disks.rowCount() >= 1
    assert monitor.processes.rowCount() == 10
    assert monitor.processes.item(0, 4).text() == "systemd"
    assert monitor.containers.rowCount() == 0  # that machine has no Docker
    assert ctx.errors == []


def test_a_result_for_a_server_that_is_no_longer_selected_is_dropped(tmp_path):
    outputs = {
        "a": COLLECTED.replace("Debian GNU/Linux 13 (trixie)", "OS-of-A"),
        "b": COLLECTED.replace("Debian GNU/Linux 13 (trixie)", "OS-of-B"),
    }
    ctx = make_ctx(
        tmp_path,
        conn_factory=lambda s: FakeConn({"##end": Result(0, outputs[s.name], "")}),
        servers=[enrolled("a", "1"), enrolled("b", "2")],
    )
    window = MainWindow(ctx)
    settle(ctx)
    window.table.selectRow(0)
    window.table.selectRow(1)
    settle(ctx)

    monitor = window.tab_list[0]
    assert monitor.server.name == "b"
    assert "OS-of-B" in monitor.summary.text()
    assert "OS-of-A" not in monitor.summary.text()


def test_a_server_without_a_key_is_not_contacted(tmp_path):
    asked = []
    ctx = make_ctx(
        tmp_path,
        conn_factory=lambda server: asked.append(server) or FakeConn(),
        servers=[Server(name="new", host="1")],
    )
    window = MainWindow(ctx)
    window.table.selectRow(0)
    settle(ctx)

    assert asked == []
    assert "Ключ ещё не установлен" in window.tab_list[0].summary.text()


def test_removing_a_server_asks_first_and_forgets_it(tmp_path):
    ctx = make_ctx(tmp_path, servers=[Server(name="old", host="1")])
    window = MainWindow(ctx)
    window.table.selectRow(0)

    window.remove_server()

    assert ctx.confirmed and "old" in ctx.confirmed[0][1]
    assert ctx.servers.load() == []
    assert window.table.rowCount() == 0


def test_a_server_that_returns_garbage_is_reported_not_crashed(tmp_path):
    ctx = make_ctx(
        tmp_path,
        conn_factory=lambda server: FakeConn({"##end": Result(0, "no sections", "")}),
        servers=[enrolled()],
    )
    window = MainWindow(ctx)
    window.table.selectRow(0)
    settle(ctx)

    assert "Нет связи" in window.tab_list[0].summary.text()
