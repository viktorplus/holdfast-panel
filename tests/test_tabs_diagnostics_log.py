import json
from datetime import UTC, datetime

import pytest
from ui_support import FakeConn, application, enrolled, make_ctx, settle

from holdfast_panel.ssh import Result, SshError
from holdfast_panel.store import Server
from holdfast_panel.ui.diagnostics_tab import DiagnosticsTab
from holdfast_panel.ui.log_tab import LogTab

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()
GIB = 1024**3


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


def server_answers(audit_findings):
    journal = json.dumps(
        {
            "last_run": {"at": "2026-10-11T02:30:00+00:00", "ok": True},
            "last_success": {"at": "2026-10-11T02:30:00+00:00", "ok": True},
        }
    )
    return {
        "command -v holdfast": Result(0, "0.3.0\n", ""),
        "holdfast config check": Result(0, "configuration is complete\n", ""),
        "date -u": Result(0, f"{NOW:.0f}\n", ""),
        "cat /var/lib/holdfast/jobs/backup.json": Result(0, journal, ""),
        "df -P": Result(0, f"/dev/sda1 {100 * GIB} {20 * GIB} {80 * GIB} 20% /\n", ""),
        "systemctl is-active cron": Result(3, "inactive\n", ""),
        "holdfast backup --dry-run": Result(0, "mode: auto\n", ""),
        "holdfast audit --json": Result(
            1, json.dumps({"findings": audit_findings}), ""
        ),
    }


def tab_with(tmp_path, factory):
    ctx = make_ctx(tmp_path, conn_factory=factory, servers=[enrolled()])
    tab = DiagnosticsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    return ctx, tab


def test_diagnostics_fills_both_tables_and_colours_the_problems(tmp_path):
    findings = [
        {
            "id": "a",
            "title": "SSH root login",
            "status": "FAIL",
            "group": "access",
            "detail": "yes",
        },
        {
            "id": "b",
            "title": "Updates",
            "status": "WARN",
            "group": "system",
            "detail": "12 pending",
        },
        {"id": "c", "title": "Fine", "status": "PASS", "group": "x"},
    ]
    ctx, tab = tab_with(tmp_path, lambda s: FakeConn(server_answers(findings)))

    assert tab.items.rowCount() == 0  # nothing runs until asked
    tab.run_diagnostics()
    settle(ctx)

    names = [tab.items.item(r, 0).text() for r in range(tab.items.rowCount())]
    assert "Планировщик (cron)" in names and "Последний бэкап" in names
    cron = names.index("Планировщик (cron)")
    assert tab.items.item(cron, 1).text() == "✖ проблема"
    assert tab.title.text() == "Итог: ✖ проблема"
    # only what needs attention is listed, failures first
    assert [tab.audit.item(r, 1).text() for r in range(tab.audit.rowCount())] == [
        "SSH root login",
        "Updates",
    ]
    assert tab.run_button.isEnabled() and ctx.errors == []


def test_a_server_without_holdfast_says_so(tmp_path):
    answers = {"command -v holdfast": Result(1, "", "")}
    ctx, tab = tab_with(tmp_path, lambda s: FakeConn(answers))
    tab.run_diagnostics()
    settle(ctx)

    assert tab.items.rowCount() == 1
    assert "не установлен" in tab.items.item(0, 2).text()


def test_a_failed_connection_is_shown_and_the_button_comes_back(tmp_path):
    def refuse(server):
        raise SshError("timed out")

    ctx, tab = tab_with(tmp_path, refuse)
    tab.run_diagnostics()
    settle(ctx)

    assert "timed out" in tab.title.text()
    assert tab.run_button.isEnabled()


def test_a_server_without_a_key_cannot_be_diagnosed(tmp_path):
    ctx = make_ctx(tmp_path, servers=[Server(name="new", host="1")])
    tab = DiagnosticsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    assert not tab.run_button.isEnabled()
    assert "Ключ ещё не установлен" in tab.title.text()


# ---- the log -----------------------------------------------------------------


def test_the_log_shows_this_servers_actions_or_everyones(tmp_path):
    ctx = make_ctx(tmp_path, servers=[enrolled("web-1", "1"), enrolled("db-1", "2")])
    ctx.log("web-1", "бэкап", True)
    ctx.log("db-1", "восстановление", False, "остановилось")
    ctx.log("web-1", "шаблон system-update", True)

    tab = LogTab(ctx)
    tab.server = ctx.servers.load()[0]  # web-1
    tab.refresh()
    assert [tab.table.item(r, 2).text() for r in range(tab.table.rowCount())] == [
        "шаблон system-update",
        "бэкап",
    ]

    tab.only_this.setChecked(False)
    assert tab.table.rowCount() == 3
    row = next(r for r in range(3) if tab.table.item(r, 1).text() == "db-1")
    assert tab.table.item(row, 3).text() == "ошибка"
    assert tab.table.item(row, 4).text() == "остановилось"


def test_an_empty_log_is_an_empty_table(tmp_path):
    ctx = make_ctx(tmp_path)
    tab = LogTab(ctx)
    tab.refresh()
    assert tab.table.rowCount() == 0
