"""Is this server healthy, and is its backup real?

Each check answers with one of four statuses and a sentence a person can act
on. The checks read; nothing here changes the server, with one exception that is
opt-in: the audit, which saves its own baseline (see `diagnose`).
"""

from __future__ import annotations

import json
import shlex
import tomllib
from dataclasses import dataclass, field
from datetime import datetime

from .ssh import Connection, SshError

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
_ORDER = {FAIL: 0, WARN: 1, UNKNOWN: 2, OK: 3}

CONFIG_PATH = "/etc/holdfast/holdfast.toml"
DEFAULT_JOBS = "/var/lib/holdfast/jobs"
BACKUP_STALE_HOURS = 36
"""A nightly backup older than a day and a half has missed a night."""
DISK_WARN_PERCENT = 85
DISK_FAIL_PERCENT = 95


@dataclass(frozen=True)
class Item:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class AuditFinding:
    id: str
    title: str
    status: str
    severity: str
    detail: str
    group: str


@dataclass
class Diagnosis:
    items: list[Item] = field(default_factory=list)
    audit: list[AuditFinding] = field(default_factory=list)

    @property
    def worst(self) -> str:
        statuses = [i.status for i in self.items]
        return min(statuses, key=_ORDER.__getitem__) if statuses else UNKNOWN


def _age_hours(iso: str, now_epoch: float) -> float:
    return (now_epoch - datetime.fromisoformat(iso).timestamp()) / 3600


def evaluate_journal(text: str, now_epoch: float) -> Item:
    """The last backup, read from /var/lib/holdfast/jobs/backup.json."""
    name = "Последний бэкап"
    if not text.strip():
        return Item(name, FAIL, "бэкап ещё ни разу не запускался (журнала нет)")
    try:
        journal = json.loads(text)
        last_run = journal.get("last_run") or {}
        last_ok = journal.get("last_success") or {}
        ran_ok = bool(last_run.get("ok"))
        success_age = _age_hours(last_ok["at"], now_epoch) if last_ok else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return Item(name, UNKNOWN, "журнал бэкапа не удалось прочитать")
    if success_age is None:
        return Item(name, FAIL, "не было ни одного успешного бэкапа")
    ago = f"{success_age:.0f} ч назад"
    if not ran_ok:
        return Item(
            name, FAIL, f"последний запуск не удался; последний успешный: {ago}"
        )
    if success_age > BACKUP_STALE_HOURS:
        return Item(name, WARN, f"последний успешный бэкап {ago}: пропущена ночь")
    return Item(name, OK, f"успешно, {ago}")


def evaluate_disk(df_line: str, where: str) -> Item:
    name = "Место под снимки"
    parts = df_line.split()
    try:
        used, avail = int(parts[2]), int(parts[3])
        # as df shows it: what a writer that is not root can still use
        percent = 100 * used / (used + avail)
    except (IndexError, ValueError, ZeroDivisionError):
        return Item(name, UNKNOWN, f"не удалось узнать размер диска для {where}")
    gib_free = avail / 1024**3
    detail = f"{where}: занято {percent:.0f}%, свободно {gib_free:.1f} ГБ"
    if percent >= DISK_FAIL_PERCENT:
        return Item(name, FAIL, detail)
    return Item(name, WARN if percent >= DISK_WARN_PERCENT else OK, detail)


def parse_audit(text: str) -> list[AuditFinding]:
    try:
        findings = json.loads(text)["findings"]
        return [
            AuditFinding(
                id=f["id"],
                title=f["title"],
                status=f["status"],
                severity=f.get("severity", ""),
                detail=f.get("detail", ""),
                group=f.get("group", ""),
            )
            for f in findings
        ]
    except (ValueError, KeyError, TypeError):
        return []


def audit_item(findings: list[AuditFinding], exit_code: int) -> Item:
    name = "Аудит безопасности (holdfast audit)"
    if not findings:
        return Item(name, UNKNOWN, f"отчёт не получен (код {exit_code})")
    failed = [f for f in findings if f.status == "FAIL"]
    warned = [f for f in findings if f.status == "WARN"]
    if failed:
        return Item(
            name,
            FAIL,
            f"провалено проверок: {len(failed)}, предупреждений: {len(warned)}",
        )
    if warned:
        return Item(name, WARN, f"предупреждений: {len(warned)}")
    return Item(name, OK, f"проверок пройдено: {len(findings)}")


def _first_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[0] if lines else ""


def _holdfast_paths(conn: Connection, backup_root: str) -> tuple[str, str]:
    """backup.root and jobs.dir as this server's holdfast.toml says.

    The defaults are only defaults: a server configured otherwise would show a
    false "no backup journal" for a journal that is somewhere else.
    """
    result = conn.run(f"cat {shlex.quote(CONFIG_PATH)} 2>/dev/null")
    try:
        config = tomllib.loads(result.out)
    except tomllib.TOMLDecodeError:
        config = {}
    root = (config.get("backup") or {}).get("root") or backup_root
    jobs = (config.get("jobs") or {}).get("dir") or DEFAULT_JOBS
    return str(root), str(jobs)


def journal_item(conn: Connection, jobs_dir: str, now_epoch: float) -> Item:
    journal = conn.run(f"cat {shlex.quote(jobs_dir + '/backup.json')}")
    if journal.ok:
        return evaluate_journal(journal.out, now_epoch)
    name = "Последний бэкап"
    if "Permission denied" in journal.err:
        return Item(name, UNKNOWN, "нет прав читать журнал: подключитесь как root")
    if "No such file" in journal.err:
        return evaluate_journal("", now_epoch)
    return Item(name, UNKNOWN, journal.err.strip() or "журнал не прочитан")


def scheduler_item(conn: Connection) -> Item:
    """Who starts the nightly backup: cron, or a systemd timer. Not finding either
    is a warning: holdfast's own docs allow both, and a schedule can live elsewhere."""
    name = "Расписание бэкапа"
    found = conn.run(
        "(systemctl is-active cron || systemctl is-active crond) 2>/dev/null"
        " | grep -qx active && echo cron"
        " || (systemctl list-timers --all --no-legend 2>/dev/null | grep -qi holdfast"
        " && echo timer)"
        " || (test -e /etc/cron.d/holdfast && echo crontab-file)"
        " || echo none"
    ).out.strip()
    if found == "cron":
        return Item(name, OK, "cron работает")
    if found == "timer":
        return Item(name, OK, "бэкап запускает systemd-таймер")
    if found == "crontab-file":
        return Item(name, WARN, "файл /etc/cron.d/holdfast есть, но cron не запущен")
    return Item(
        name,
        WARN,
        "ни cron, ни таймера holdfast не найдено: ночной бэкап может не запускаться",
    )


def diagnose(
    conn: Connection, backup_root: str = "/opt/backups", with_audit: bool = False
) -> Diagnosis:
    """Run the checks. A check that cannot run is UNKNOWN, never silently OK.

    The audit is separate and off by default: `holdfast audit` saves its report
    as the baseline the next audit compares with, so running it from here would
    use up changes the server's own nightly audit is supposed to report. Every
    other check only reads.
    """
    result = Diagnosis()
    add = result.items.append

    version = conn.run("command -v holdfast >/dev/null && holdfast version")
    if not version.ok:
        add(Item("holdfast", FAIL, "holdfast не установлен на сервере"))
        return result
    add(Item("holdfast", OK, f"версия {version.out.strip()}"))

    config = conn.run("holdfast config check")
    add(
        Item("Конфигурация", OK, "всё на месте")
        if config.ok
        else Item("Конфигурация", WARN, _first_line(config.out) or config.err.strip())
    )

    now = conn.run("date -u +%s").out.strip()
    try:
        now_epoch = float(now)
    except ValueError as exc:
        raise SshError("the server did not report its time") from exc
    root, jobs_dir = _holdfast_paths(conn, backup_root)
    add(journal_item(conn, jobs_dir, now_epoch))

    disk = conn.run(f"df -P -B1 {shlex.quote(root)} 2>/dev/null | tail -n 1")
    add(evaluate_disk(disk.out, root))
    add(scheduler_item(conn))

    dry = conn.run("holdfast backup --dry-run", timeout=120)
    if dry.ok:
        warnings = [
            ln for ln in dry.out.splitlines() if "the backup would refuse" in ln
        ]
        add(
            Item("Состав копии (--dry-run)", WARN, warnings[0].strip())
            if warnings
            else Item("Состав копии (--dry-run)", OK, "бэкап запустится")
        )
    else:
        add(Item("Состав копии (--dry-run)", FAIL, _first_line(dry.err) or "ошибка"))

    if with_audit:
        audit = conn.run("holdfast audit --json", timeout=300)
        result.audit = parse_audit(audit.out)
        add(audit_item(result.audit, 0 if audit.ok else 1))
    else:
        add(
            Item(
                "Аудит безопасности (holdfast audit)",
                UNKNOWN,
                "не запускался: отметьте «Включить аудит» (он обновляет сохранённое "
                "состояние, с которым сравнивает ночной аудит)",
            )
        )
    return result
