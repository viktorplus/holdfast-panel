"""Each test here fails on the code before the review fixes it guards."""

import threading
import time

import pytest
from test_tab_contents import REAL
from test_tab_contents import Server as ConfigServer
from ui_support import COLLECTED, FakeConn, application, enrolled, make_ctx, settle

from holdfast_panel import actionlog
from holdfast_panel.restore_flow import Outcome
from holdfast_panel.ssh import Result
from holdfast_panel.store import Server
from holdfast_panel.ui.backups_tab import BackupsTab
from holdfast_panel.ui.contents_tab import ContentsTab
from holdfast_panel.ui.main_window import MainWindow
from holdfast_panel.ui.monitor_tab import MonitorTab


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


# ---- unsaved edits are not thrown away ----------------------------------------------


def contents_with_two_servers(tmp_path):
    config = ConfigServer(REAL)
    ctx = make_ctx(
        tmp_path,
        conn_factory=lambda s: config,
        servers=[enrolled("a", "1"), enrolled("b", "2")],
    )
    tab = ContentsTab(ctx)
    a, b = ctx.servers.load()
    tab.server = a
    tab.refresh()
    settle(ctx)
    return ctx, tab, a, b


def test_a_refresh_keeps_edits_that_were_not_saved(tmp_path):
    ctx, tab, _a, _b = contents_with_two_servers(tmp_path)
    tab.editor.setPlainText("# my edit\n" + REAL)

    tab.refresh()  # what switching to another tab and back does
    settle(ctx)

    assert tab.editor.toPlainText().startswith("# my edit")
    assert "несохранённые" in tab.message.text()


def test_edits_wait_for_their_server_while_another_is_shown(tmp_path):
    ctx, tab, a, b = contents_with_two_servers(tmp_path)
    tab.editor.setPlainText("# only on a\n" + REAL)

    tab.server = b
    tab.refresh()
    settle(ctx)
    assert tab.editor.toPlainText() == REAL

    tab.server = a
    tab.refresh()
    settle(ctx)
    assert tab.editor.toPlainText().startswith("# only on a")
    assert "Восстановлены" in tab.message.text()


def test_the_load_button_discards_edits_on_purpose(tmp_path):
    ctx, tab, _a, _b = contents_with_two_servers(tmp_path)
    tab.editor.setPlainText("# my edit\n" + REAL)

    tab.reload_from_server()
    settle(ctx)

    assert tab.editor.toPlainText() == REAL


# ---- a worker waiting for an answer is never stuck ------------------------------------


def test_a_dialog_that_blows_up_answers_no_instead_of_hanging_the_worker(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    tab = ContentsTab(ctx)
    tab.server = ctx.servers.load()[0]
    answers = []

    def boom(title, text):
        raise RuntimeError("dialog failed")

    ctx.confirm = boom

    def fn(on_text, on_stage):
        answers.append(tab.ask("q", "t"))
        return Outcome(True, "x")

    tab.operate("ask", fn)
    settle(ctx)

    assert answers == [False]


def test_closing_the_window_releases_a_worker_waiting_for_an_answer(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    tab = ContentsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab._signals.question.disconnect(tab._answer_question)  # nobody answers
    answers = []

    def fn(on_text, on_stage):
        answers.append(tab.ask("q", "t"))
        return Outcome(True, "x")

    tab.operate("ask", fn)
    time.sleep(0.3)
    application().processEvents()
    assert answers == []  # still waiting

    tab.closing = True
    settle(ctx)

    assert answers == [False]


def test_closing_while_something_runs_asks_and_can_be_cancelled(tmp_path):
    from PySide6.QtGui import QCloseEvent

    ctx = make_ctx(tmp_path, servers=[enrolled()])
    window = MainWindow(ctx)
    settle(ctx)
    tab = window.tab_list[1]  # Бэкапы
    tab.busy = True

    ctx.confirm = lambda title, text: False
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and not tab.closing

    ctx.confirm = lambda title, text: True
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and tab.closing


# ---- a result belongs to the server it was started on ----------------------------------


def test_what_follows_an_operation_is_skipped_when_another_server_is_shown(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled("a", "1"), enrolled("b", "2")])
    tab = ContentsTab(ctx)
    a, b = ctx.servers.load()
    tab.server = a
    release = threading.Event()
    after = []

    def fn(on_text, on_stage):
        release.wait(5)
        return Outcome(True, "x", "saved")

    tab.operate("save", fn, after=after.append)
    tab.server = b
    release.set()
    settle(ctx)

    assert after == []
    # and it was still written to the log, under the server it ran on
    assert actionlog.read(ctx.log_path)[0].server == "a"


# ---- buttons follow one rule ------------------------------------------------------------


def test_buttons_stay_off_for_a_server_without_a_key_after_an_operation(tmp_path):
    ctx = make_ctx(tmp_path, servers=[Server(name="new", host="1")])
    tab = ContentsTab(ctx)
    tab.server = ctx.servers.load()[0]

    tab.set_busy(True)
    tab.set_busy(False)

    assert not tab.save_button.isEnabled() and not tab.plan_button.isEnabled()


def test_the_place_selector_is_off_while_an_operation_runs_and_back_after(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    tab = BackupsTab(ctx)
    tab.server = ctx.servers.load()[0]

    tab.set_busy(True)
    assert not tab.where.isEnabled() and not tab.storages_button.isEnabled()
    tab.set_busy(False)
    assert tab.where.isEnabled() and tab.storages_button.isEnabled()


# ---- auto-refresh is polite -------------------------------------------------------------------


def test_the_timer_does_nothing_for_a_hidden_tab_or_on_top_of_a_request(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])
    tab = MonitorTab(ctx)
    calls = []
    tab.refresh = lambda: calls.append(1)

    tab._tick()  # not shown
    assert calls == []

    tab.show()
    tab.reading = True  # a request is still on its way
    tab._tick()
    assert calls == []

    tab.reading = False
    tab._tick()
    assert calls == [1]
    tab.hide()


def test_a_server_is_not_asked_again_while_its_check_is_running(tmp_path):
    asked = []
    release = threading.Event()

    def factory(server):
        asked.append(server.name)
        release.wait(5)
        return FakeConn({"##end": Result(0, COLLECTED, "")})

    ctx = make_ctx(tmp_path, conn_factory=factory, servers=[enrolled("a", "1")])
    window = MainWindow(ctx)  # starts the first check
    window.refresh_all()
    window.refresh_all()
    time.sleep(0.2)
    release.set()
    settle(ctx)

    assert asked == ["a"]


# ---- the password does not stay in the form ----------------------------------------------------


def test_the_password_is_not_left_in_the_dialog_after_it_was_taken(
    tmp_path, monkeypatch
):
    from holdfast_panel.ui import main_window
    from holdfast_panel.ui.add_dialog import AddServerDialog

    seen = {}

    def fake_exec(dialog):
        dialog.name.setText("x")
        dialog.host.setText("127.0.0.1")
        dialog.password.setText("hunter2")
        seen["dialog"] = dialog
        return AddServerDialog.DialogCode.Accepted

    monkeypatch.setattr(AddServerDialog, "exec", fake_exec)
    monkeypatch.setattr(main_window, "fetch_host_fingerprint", lambda h, p: "SHA256:x")
    ctx = make_ctx(tmp_path)
    window = MainWindow(ctx)

    window.add_server()

    assert seen["dialog"].password.text() == ""


# ---- text from the server is text, not markup ------------------------------------------------


def test_a_hostile_os_name_does_not_become_html_in_the_monitor(tmp_path):
    hostile = COLLECTED.replace(
        "Debian GNU/Linux 13 (trixie)", "<img src='http://evil/x.png'><b>owned</b>"
    )
    ctx = make_ctx(
        tmp_path,
        conn_factory=lambda s: FakeConn({"##end": Result(0, hostile, "")}),
        servers=[enrolled()],
    )
    tab = MonitorTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    settle(ctx)

    shown = tab.summary.text()
    assert "<img" not in shown and "&lt;img" in shown


def test_what_could_not_be_read_is_shown_not_hidden(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled()])  # the real Debian output: no Docker
    tab = MonitorTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    settle(ctx)

    assert "Docker есть, но не отвечает" in tab.summary.text()
