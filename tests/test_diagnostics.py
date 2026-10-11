import json
from datetime import UTC, datetime

from holdfast_panel.diagnostics import (
    FAIL,
    OK,
    UNKNOWN,
    WARN,
    AuditFinding,
    audit_item,
    diagnose,
    evaluate_disk,
    evaluate_journal,
    parse_audit,
)
from holdfast_panel.ssh import Result

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()


def journal(last_run_ok, last_success_at, run_at="2026-10-11T02:30:00+00:00"):
    entry = {"at": run_at, "ok": last_run_ok}
    return json.dumps(
        {
            "kind": "backup",
            "last_run": entry,
            "last_success": {"at": last_success_at, "ok": True}
            if last_success_at
            else None,
        }
    )


# ---- the last backup -------------------------------------------------------


def test_a_backup_from_last_night_is_fine():
    item = evaluate_journal(journal(True, "2026-10-11T02:30:00+00:00"), NOW)
    assert item.status == OK and "10 ч" in item.detail


def test_a_missed_night_is_a_warning():
    item = evaluate_journal(journal(True, "2026-10-09T02:30:00+00:00"), NOW)
    assert item.status == WARN


def test_a_failed_last_run_is_a_failure_even_with_an_older_success():
    item = evaluate_journal(journal(False, "2026-10-10T02:30:00+00:00"), NOW)
    assert item.status == FAIL and "не удался" in item.detail


def test_no_journal_and_no_success_are_failures_and_garbage_is_unknown():
    assert evaluate_journal("", NOW).status == FAIL
    assert evaluate_journal(journal(False, None), NOW).status == FAIL
    assert evaluate_journal("{nope", NOW).status == UNKNOWN
    assert evaluate_journal('{"last_run": 5}', NOW).status == UNKNOWN


# ---- disk and audit --------------------------------------------------------


def test_disk_thresholds():
    gib = 1024**3
    assert (
        evaluate_disk(f"/dev/sda1 {100 * gib} {50 * gib} {50 * gib} 50% /", "/x").status
        == OK
    )
    assert (
        evaluate_disk(f"/dev/sda1 {100 * gib} {90 * gib} {10 * gib} 90% /", "/x").status
        == WARN
    )
    assert (
        evaluate_disk(f"/dev/sda1 {100 * gib} {97 * gib} {3 * gib} 97% /", "/x").status
        == FAIL
    )
    assert evaluate_disk("", "/x").status == UNKNOWN


def test_an_audit_report_is_read_and_summarised():
    text = json.dumps(
        {
            "findings": [
                {"id": "a", "title": "A", "status": "PASS", "group": "g"},
                {
                    "id": "b",
                    "title": "B",
                    "status": "FAIL",
                    "severity": "high",
                    "detail": "d",
                },
                {"id": "c", "title": "C", "status": "WARN"},
            ]
        }
    )
    findings = parse_audit(text)
    assert [f.id for f in findings] == ["a", "b", "c"]
    item = audit_item(findings, 1)
    assert item.status == FAIL and "1" in item.detail
    assert audit_item([AuditFinding("a", "A", "PASS", "", "", "")], 0).status == OK
    assert audit_item([], 2).status == UNKNOWN
    assert parse_audit("not json") == []


# ---- the whole run ---------------------------------------------------------


class Canned:
    """Answers commands by what they start with; records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def run(self, command, timeout=60):
        self.asked.append(command)
        for prefix, result in self.answers.items():
            if command.startswith(prefix):
                return result
        return Result(127, "", "unexpected: " + command)


def healthy():
    gib = 1024**3
    return {
        "command -v holdfast": Result(0, "0.3.0\n", ""),
        "holdfast config check": Result(0, "configuration is complete\n", ""),
        "date -u": Result(0, f"{NOW:.0f}\n", ""),
        "cat /var/lib/holdfast/jobs/backup.json": Result(
            0, journal(True, "2026-10-11T02:30:00+00:00"), ""
        ),
        "df -P": Result(0, f"/dev/sda1 {100 * gib} {20 * gib} {80 * gib} 20% /\n", ""),
        "(systemctl is-active cron": Result(0, "cron\n", ""),
        "cat /etc/holdfast/holdfast.toml": Result(0, "", ""),
        "holdfast backup --dry-run": Result(0, "mode: auto\n", ""),
        "holdfast audit --json": Result(
            0,
            json.dumps({"findings": [{"id": "a", "title": "A", "status": "PASS"}]}),
            "",
        ),
    }


def test_a_healthy_server_is_ok_on_every_line():
    diagnosis = diagnose(Canned(healthy()), with_audit=True)
    assert {i.status for i in diagnosis.items} == {OK}
    by_name = {i.name: i.status for i in diagnosis.items}
    assert by_name["Последний бэкап"] == OK
    assert by_name["Расписание бэкапа"] == OK
    assert by_name["Состав копии (--dry-run)"] == OK


def test_a_server_without_holdfast_is_a_failure_and_nothing_else_is_asked():
    conn = Canned({"command -v holdfast": Result(1, "", "")})
    diagnosis = diagnose(conn)

    assert diagnosis.worst == FAIL
    assert [i.name for i in diagnosis.items] == ["holdfast"]
    assert len(conn.asked) == 1


def test_a_stopped_cron_and_a_refusing_dry_run_are_reported():
    answers = healthy()
    answers["(systemctl is-active cron"] = Result(0, "none\n", "")
    answers["holdfast backup --dry-run"] = Result(
        0, "mode: auto\nwarnings:\n  the backup would refuse: no age on PATH\n", ""
    )
    by_name = {i.name: i for i in diagnose(Canned(answers)).items}

    # not a failure: holdfast's docs allow a systemd timer, and a schedule can
    # live somewhere this check does not look
    assert by_name["Расписание бэкапа"].status == WARN
    assert by_name["Состав копии (--dry-run)"].status == WARN
    assert "no age" in by_name["Состав копии (--dry-run)"].detail
