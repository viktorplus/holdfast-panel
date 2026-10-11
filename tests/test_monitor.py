from pathlib import Path

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash

from holdfast_panel.monitor import (
    collect,
    human_bytes,
    human_uptime,
    parse_stats,
)
from holdfast_panel.ssh import SshError, connect, fetch_host_fingerprint
from holdfast_panel.store import Server

FIXTURE = Path(__file__).parent / "fixtures" / "collected_debian_wsl.txt"


def test_real_output_from_a_debian_machine_is_understood():
    stats = parse_stats(FIXTURE.read_text(encoding="utf-8"))

    assert stats.os_name == "Debian GNU/Linux 13 (trixie)"
    assert stats.kernel.startswith("6.")
    assert stats.cpus == 8
    assert stats.load == (0.35, 0.41, 0.52)
    assert stats.mem_total == 16263540 * 1024
    assert 5 < stats.mem_percent < 10
    # 18 of 22 non-idle jiffies: the delta between the two /proc/stat lines.
    assert 0 < stats.cpu_percent < 100
    assert stats.uptime_seconds > 400000
    assert [p.command for p in stats.processes][:2] == ["systemd", "bash"]
    assert stats.processes[0].pid == 1 and stats.processes[0].cpu == 6.6
    assert not any(d.mount.endswith("cli-tools") for d in stats.disks)  # loop image
    root = next(d for d in stats.disks if d.mount == "/")
    assert round(root.percent) == 1
    # "docker: not found" hint text must not become a container
    assert stats.containers == []
    assert stats.holdfast_version == ""


def test_containers_and_the_holdfast_version_are_read():
    stats = parse_stats(
        "##docker\n"
        "myapp-db-1|mysql:8.4|Up 3 days (healthy)\n"
        "myapp-wp-1|wordpress:latest|Up 3 days\n"
        "##holdfast\n0.3.0\n##end\n"
    )
    assert [(c.name, c.image) for c in stats.containers] == [
        ("myapp-db-1", "mysql:8.4"),
        ("myapp-wp-1", "wordpress:latest"),
    ]
    assert stats.containers[0].status == "Up 3 days (healthy)"
    assert stats.holdfast_version == "0.3.0"


def test_cpu_percent_is_the_busy_share_between_two_samples():
    stats = parse_stats(
        "##stat\ncpu  100 0 100 700 100 0 0 0 0 0\ncpu  150 0 150 750 100 0 0 0 0 0\n"
    )
    # 150 jiffies passed, 50 of them idle -> 100 busy of 150
    assert round(stats.cpu_percent, 1) == 66.7


def test_a_proc_stat_that_is_not_linux_is_zero_not_a_crash():
    assert parse_stats("##stat\ncpu 1 2\ncpu 3 4\n").cpu_percent == 0.0


def test_a_server_that_returns_nothing_gives_empty_stats_not_a_crash():
    stats = parse_stats("##end\n")
    assert stats.cpus == 1 and stats.disks == [] and stats.mem_percent == 0.0


def test_the_same_device_mounted_twice_is_listed_once():
    stats = parse_stats(
        "##disk\n"
        "/dev/sda1 1000 500 500 50% /\n"
        "/dev/sda1 1000 500 500 50% /var/lib/docker/x\n"
    )
    assert [d.mount for d in stats.disks] == ["/"]


def test_numbers_read_as_people_expect():
    assert human_bytes(512) == "512 Б"
    assert human_bytes(1536) == "1.5 КБ"
    assert human_bytes(3 * 1024**3) == "3.0 ГБ"
    assert human_uptime(59 * 60) == "59 мин"
    assert human_uptime(3 * 3600 + 120) == "3 ч 2 мин"
    assert human_uptime(2 * 86400 + 5 * 3600) == "2 д 5 ч"


@pytest.mark.skipif(find_bash() is None, reason="needs bash")
def test_collect_runs_over_ssh_and_stops_on_a_dead_reply(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sshd = FakeSshd(home)
    server = Server(
        name="t",
        host="127.0.0.1",
        port=sshd.port,
        host_fingerprint=fetch_host_fingerprint("127.0.0.1", sshd.port),
    )
    with connect(server, password=PASSWORD) as conn:
        # Git Bash has no real /proc, so only the shape is checked here.
        assert collect(conn).cpus >= 1

        class Dead:
            def run(self, *a, **k):
                from holdfast_panel.ssh import Result

                return Result(127, "", "bash: not found")

        with pytest.raises(SshError, match="did not return"):
            collect(Dead())
    sshd.close()
