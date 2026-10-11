"""Findings of the review of diagnostics, alerts and the action log."""

import json
import threading
from datetime import UTC, datetime

from holdfast_panel import actionlog
from holdfast_panel.alerts import evaluate
from holdfast_panel.diagnostics import (
    FAIL,
    OK,
    UNKNOWN,
    WARN,
    Diagnosis,
    Item,
    diagnose,
    evaluate_disk,
    journal_item,
    scheduler_item,
)
from holdfast_panel.monitor import Disk, Stats
from holdfast_panel.ssh import Result

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()
GIB = 1024**3


class Canned:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def run(self, command, timeout=60):
        self.asked.append(command)
        for prefix, result in self.answers.items():
            if command.startswith(prefix):
                return result
        return Result(127, "", "unexpected: " + command)


# ---- who starts the backup --------------------------------------------------------------------


def schedule(output):
    return scheduler_item(Canned({"(systemctl": Result(0, output + "\n", "")}))


def test_a_systemd_timer_is_a_valid_schedule_not_a_failure():
    item = schedule("timer")
    assert item.status == OK and "таймер" in item.detail


def test_cron_is_a_valid_schedule():
    assert schedule("cron").status == OK


def test_a_crontab_file_without_a_running_cron_is_a_warning():
    assert schedule("crontab-file").status == WARN


def test_no_schedule_found_is_a_warning_never_a_failure():
    item = schedule("none")
    assert item.status == WARN  # the schedule may live somewhere this does not look


# ---- the journal ---------------------------------------------------------------------------------


def test_the_journal_is_read_from_where_the_servers_own_config_says():
    conn = Canned(
        {
            "cat /etc/holdfast/holdfast.toml": Result(
                0,
                '[jobs]\ndir = "/srv/holdfast/jobs"\n[backup]\nroot = "/srv/snaps"\n',
                "",
            ),
            "date -u": Result(0, f"{NOW:.0f}\n", ""),
            "cat /srv/holdfast/jobs/backup.json": Result(
                0,
                json.dumps(
                    {
                        "last_run": {"at": "2026-10-11T02:00:00+00:00", "ok": True},
                        "last_success": {"at": "2026-10-11T02:00:00+00:00", "ok": True},
                    }
                ),
                "",
            ),
            "command -v holdfast": Result(0, "0.3.0\n", ""),
            "holdfast config check": Result(0, "", ""),
            "df -P": Result(0, f"/dev/a {100 * GIB} {10 * GIB} {90 * GIB} 10% /\n", ""),
            "(systemctl": Result(0, "cron\n", ""),
            "holdfast backup --dry-run": Result(0, "ok\n", ""),
        }
    )

    by_name = {i.name: i for i in diagnose(conn).items}

    assert by_name["Последний бэкап"].status == OK
    assert "/srv/snaps" in by_name["Место под снимки"].detail  # the real root, too


def test_a_journal_that_a_non_root_user_cannot_read_is_unknown_not_a_failure():
    conn = Canned({"cat ": Result(1, "", "cat: backup.json: Permission denied")})
    item = journal_item(conn, "/var/lib/holdfast/jobs", NOW)
    assert item.status == UNKNOWN and "root" in item.detail


def test_a_journal_that_does_not_exist_is_a_failure():
    conn = Canned(
        {"cat ": Result(1, "", "cat: backup.json: No such file or directory")}
    )
    assert journal_item(conn, "/var/lib/holdfast/jobs", NOW).status == FAIL


# ---- the audit changes the server's baseline, so it is opt-in ------------------------------------


def healthy_answers():
    return {
        "command -v holdfast": Result(0, "0.3.0\n", ""),
        "holdfast config check": Result(0, "", ""),
        "date -u": Result(0, f"{NOW:.0f}\n", ""),
        "cat /etc/holdfast/holdfast.toml": Result(0, "", ""),
        "cat /var/lib/holdfast/jobs/backup.json": Result(
            0,
            json.dumps(
                {
                    "last_run": {"at": "2026-10-11T02:00:00+00:00", "ok": True},
                    "last_success": {"at": "2026-10-11T02:00:00+00:00", "ok": True},
                }
            ),
            "",
        ),
        "df -P": Result(0, f"/dev/a {100 * GIB} {10 * GIB} {90 * GIB} 10% /\n", ""),
        "(systemctl": Result(0, "cron\n", ""),
        "holdfast backup --dry-run": Result(0, "ok\n", ""),
        "holdfast audit --json": Result(
            0,
            json.dumps({"findings": [{"id": "a", "title": "A", "status": "PASS"}]}),
            "",
        ),
    }


def test_the_audit_is_not_run_unless_asked_because_it_saves_a_baseline():
    conn = Canned(healthy_answers())

    diagnosis = diagnose(conn)

    assert not any("holdfast audit" in c for c in conn.asked)
    audit = next(i for i in diagnosis.items if i.name.startswith("Аудит"))
    assert audit.status == UNKNOWN and "обновляет" in audit.detail


def test_the_audit_runs_when_asked():
    conn = Canned(healthy_answers())
    diagnosis = diagnose(conn, with_audit=True)
    assert any(c == "holdfast audit --json" for c in conn.asked)
    assert next(i for i in diagnosis.items if i.name.startswith("Аудит")).status == OK


def test_the_diagnostics_tab_does_not_run_the_audit_by_default(tmp_path):
    from ui_support import FakeConn, application, enrolled, make_ctx, settle

    application()  # a widget needs a QApplication; without one the process dies

    conn = FakeConn(healthy_answers())
    ctx = make_ctx(tmp_path, conn_factory=lambda s: conn, servers=[enrolled()])
    from holdfast_panel.ui.diagnostics_tab import DiagnosticsTab

    tab = DiagnosticsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    tab.run_diagnostics()
    settle(ctx)

    assert not any("holdfast audit" in c for c in conn.log)
    assert not tab.with_audit.isChecked()


# ---- disk fullness -----------------------------------------------------------------------------------


def test_reserved_blocks_do_not_make_a_full_disk_look_95_percent():
    # df shows 100%: used 95 of 100, nothing available (5% reserved for root)
    line = f"/dev/a {100 * GIB} {95 * GIB} 0 100% /"
    assert evaluate_disk(line, "/x").status == FAIL


def test_free_space_is_what_a_writer_can_use():
    line = f"/dev/a {100 * GIB} {50 * GIB} {40 * GIB} 56% /"  # 10 GiB reserved
    item = evaluate_disk(line, "/x")
    assert "свободно 40.0 ГБ" in item.detail


def test_the_same_disk_is_not_alerted_twice():
    stats = Stats(
        cpus=4, disks=[Disk("/", 100, 95, 5)], mem_total=100, mem_available=90
    )
    diagnosis = Diagnosis([Item("Место под снимки", FAIL, "/: занято 95%")])

    alerts = evaluate(stats, diagnosis)

    assert [a.text for a in alerts] == ["диск / заполнен на 95%"]


def test_the_backup_disk_is_still_reported_when_the_monitor_has_no_disks():
    diagnosis = Diagnosis([Item("Место под снимки", FAIL, "/x: занято 99%")])
    assert any("99%" in a.text for a in evaluate(None, diagnosis))


# ---- the action log --------------------------------------------------------------------------------------


def test_many_threads_appending_never_interleave_a_line(tmp_path):
    path = tmp_path / "actions.jsonl"

    def work(n):
        for i in range(40):
            actionlog.record(path, f"s{n}", "action " + "x" * 300, True, f"detail {i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(actionlog.read(path, limit=10_000)) == 320  # every line intact


def test_a_log_with_undecodable_bytes_is_still_readable(tmp_path):
    path = tmp_path / "actions.jsonl"
    actionlog.record(path, "web-1", "good", True)
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe broken line\n")
    actionlog.record(path, "web-1", "after", True)

    assert [e.action for e in actionlog.read(path)] == ["after", "good"]


def test_the_log_does_not_grow_without_bound(tmp_path, monkeypatch):
    path = tmp_path / "actions.jsonl"
    monkeypatch.setattr(actionlog, "MAX_BYTES", 2000)
    for i in range(100):
        actionlog.record(path, "s", f"action {i}", True, "d" * 50)

    assert path.stat().st_size < 4000
    assert path.with_suffix(".1.jsonl").exists()
    assert actionlog.read(path)[0].action == "action 99"  # the newest is never lost
