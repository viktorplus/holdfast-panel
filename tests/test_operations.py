from datetime import UTC, datetime

from holdfast_panel import actionlog
from holdfast_panel.alerts import evaluate, worst
from holdfast_panel.diagnostics import FAIL, OK, WARN, Diagnosis, Item
from holdfast_panel.group import run_on_servers
from holdfast_panel.monitor import Disk, Stats
from holdfast_panel.ssh import SshError
from holdfast_panel.store import Server
from holdfast_panel.templates import parse

# ---- action log --------------------------------------------------------------


def test_the_log_returns_newest_first_and_filters_by_server(tmp_path):
    path = tmp_path / "log" / "actions.jsonl"
    when = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    actionlog.record(path, "web-1", "бэкап", True, now=when)
    actionlog.record(path, "db-1", "обновление", False, "шаг 2", now=when)
    actionlog.record(path, "web-1", "восстановление", True, now=when)

    everything = actionlog.read(path)
    assert [e.action for e in everything] == ["восстановление", "обновление", "бэкап"]
    assert [e.action for e in actionlog.read(path, server="web-1")] == [
        "восстановление",
        "бэкап",
    ]
    assert actionlog.read(path, limit=1)[0].action == "восстановление"
    assert everything[1].ok is False and everything[1].detail == "шаг 2"
    assert everything[0].at == "2026-10-11T12:00:00+00:00"


def test_a_missing_or_damaged_log_is_not_a_crash(tmp_path):
    path = tmp_path / "actions.jsonl"
    assert actionlog.read(path) == []
    actionlog.record(path, "s", "a", True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    assert [e.action for e in actionlog.read(path)] == ["a"]


# ---- alerts ------------------------------------------------------------------


def healthy_stats():
    return Stats(
        cpus=4,
        load=(0.5, 0.5, 0.5),
        mem_total=100,
        mem_available=60,
        disks=[Disk("/", 100, 40)],
    )


def test_a_healthy_server_has_nothing_to_report():
    diagnosis = Diagnosis([Item("Последний бэкап", OK, "успешно")])
    alerts = evaluate(healthy_stats(), diagnosis)
    assert alerts == [] and worst(alerts) == OK


def test_an_unreachable_server_is_one_failure():
    alerts = evaluate(None, None, reachable=False)
    assert [a.level for a in alerts] == [FAIL] and worst(alerts) == FAIL


def test_a_full_disk_memory_load_and_a_stale_backup_are_all_reported():
    stats = healthy_stats()
    stats.disks = [Disk("/", 100, 96), Disk("/data", 100, 88)]
    stats.mem_available = 5
    stats.load = (9.0, 5.0, 3.0)
    diagnosis = Diagnosis(
        [Item("Последний бэкап", WARN, "пропущена ночь"), Item("cron", OK, "")]
    )

    alerts = evaluate(stats, diagnosis)

    texts = [a.text for a in alerts]
    assert any(
        "/ заполнен на 96%" in t and a.level == FAIL
        for a, t in zip(alerts, texts, strict=True)
    )
    assert any("/data" in t for t in texts) and any("память" in t for t in texts)
    assert any("нагрузка" in t for t in texts) and any(
        "пропущена ночь" in t for t in texts
    )
    assert not any("cron" in t for t in texts)
    assert worst(alerts) == FAIL


# ---- running a template on several servers ----------------------------------


class Conn:
    def __init__(self, code):
        self.code = code

    def run_stream(self, command, on_text, timeout=0):
        on_text("out")
        return self.code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def test_one_failing_server_does_not_stop_the_others(tmp_path):
    template = parse({"id": "t", "name": "T", "steps": [{"title": "a", "run": "true"}]})
    servers = [
        Server(name="ok-1", host="1"),
        Server(name="down", host="2"),
        Server(name="bad", host="3"),
        Server(name="ok-2", host="4"),
    ]

    def open_connection(server):
        if server.name == "down":
            raise SshError("cannot connect")
        return Conn(1 if server.name == "bad" else 0)

    seen = []
    results = run_on_servers(
        servers,
        template,
        {},
        open_connection,
        tmp_path / "log.jsonl",
        lambda server, kind, text: seen.append((server, kind)),
    )

    assert [(r.server, r.ok) for r in results] == [
        ("ok-1", True),
        ("down", False),
        ("bad", False),
        ("ok-2", True),
    ]
    assert results[1].detail == "cannot connect"
    assert "остановилось на шаге «a»" in results[2].detail
    assert [s for s, k in seen if k == "server"] == ["ok-1", "down", "bad", "ok-2"]
    logged = actionlog.read(tmp_path / "log.jsonl")
    assert len(logged) == 4 and [e.ok for e in reversed(logged)] == [
        True,
        False,
        False,
        True,
    ]
