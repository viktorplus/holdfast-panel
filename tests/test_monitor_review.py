"""Findings of the review of the monitor: each test fails on the code before the fix."""

import pytest

from holdfast_panel.monitor import COLLECT_SCRIPT, parse_stats


@pytest.mark.parametrize(
    "text",
    [
        "##uptime\n123,45\n",
        "##uptime\ncut: /proc/uptime: No such file\n",
        "##load\n0.1 0.2\n",
        "##cpus\nmany\n",
        "##mem\nMemTotal: lots kB\n",
        "##procs\nabc root 1.0 0.1 bash\n",
        "##procs\n12 root 1,5 0,1 bash\n",
    ],
)
def test_a_value_that_does_not_parse_degrades_that_section_and_says_so(text):
    stats = parse_stats(text + "##end\n")
    assert stats.problems and stats.cpus >= 1  # nothing raised; the rest is intact


def test_one_bad_section_does_not_lose_the_others():
    stats = parse_stats(
        "##uptime\n123,45\n##load\n0.5 0.4 0.3 1/2 3\n##cpus\n4\n##end\n"
    )
    assert stats.load == (0.5, 0.4, 0.3) and stats.cpus == 4
    assert any("время работы" in p for p in stats.problems)


def test_an_old_kernel_without_memavailable_is_not_reported_as_full():
    stats = parse_stats(
        "##mem\nMemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 300 kB\n##end\n"
    )
    assert stats.mem_percent == 50.0  # (1000 - (100 + 100 + 300)) / 1000, not 100%


def test_guest_time_is_not_counted_twice():
    # user +50 already includes guest +50; idle +50. Busy is 50 of 100, not 66.7.
    stats = parse_stats(
        "##stat\ncpu 100 0 0 100 0 0 0 0 100 0\ncpu 150 0 0 150 0 0 0 0 150 0\n##end\n"
    )
    assert stats.cpu_percent == 50.0


def test_disk_fullness_is_what_df_shows_not_what_the_reserved_blocks_leave():
    # ext4 keeps 5% for root: used 95 and nothing available of 100 is full
    stats = parse_stats("##disk\n/dev/sda1 100 95 0 100% /\n##end\n")
    disk = stats.disks[0]
    assert disk.percent == 100.0 and disk.free == 0


def test_a_failed_df_or_ps_is_a_problem_not_an_empty_list():
    stats = parse_stats("##disk\n##disk-failed\n##procs\n##procs-failed\n##end\n")
    assert stats.disks == [] and stats.processes == []
    assert any("диски" in p for p in stats.problems)
    assert any("процессы" in p for p in stats.problems)


def test_partial_df_output_keeps_the_disks_it_did_list():
    stats = parse_stats("##disk\n/dev/sda1 1000 500 500 50% /\n##disk-failed\n##end\n")
    assert [d.mount for d in stats.disks] == ["/"]
    assert any("не полностью" in p for p in stats.problems)


def test_a_docker_that_does_not_answer_is_not_an_empty_container_list():
    stats = parse_stats("##docker\n##docker-failed\n##end\n")
    assert stats.containers == []
    assert any("Docker есть, но не отвечает" in p for p in stats.problems)


def test_the_collector_sets_a_fixed_locale_so_numbers_have_a_decimal_point():
    assert COLLECT_SCRIPT.lstrip().startswith("export LC_ALL=C")


def test_ps_is_not_piped_into_head_where_a_sigpipe_would_look_like_a_failure():
    # `ps | head -n 10` exits 141 on a busy machine; the output is taken first
    assert "ps -eo" in COLLECT_SCRIPT and "| head -n 10" in COLLECT_SCRIPT
    assert "procs=$(ps -eo" in COLLECT_SCRIPT
