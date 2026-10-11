"""What a server is doing right now: one SSH round trip, one script.

The script only reads (`/proc`, `df`, `ps`, `docker ps`) and prints sections
that start with `##name`. `parse_stats` turns that text into numbers, and
tolerates a missing section: a server without Docker simply has no containers.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from .ssh import Connection, SshError

COLLECT_SCRIPT = r"""
echo '##os'
( . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}" ); uname -r
echo '##uptime'; cut -d' ' -f1 /proc/uptime
echo '##load'; cat /proc/loadavg
echo '##cpus'; nproc
echo '##stat'; head -n1 /proc/stat; sleep 1; head -n1 /proc/stat
echo '##mem'; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
echo '##disk'
df -P -B1 -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2
echo '##procs'
ps -eo pid,user:20,pcpu,pmem,comm --sort=-pcpu --no-headers 2>/dev/null | head -n 10
echo '##docker'
docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null
echo '##holdfast'
command -v holdfast >/dev/null && holdfast version
echo '##end'
"""


@dataclass
class Disk:
    mount: str
    total: int
    used: int

    @property
    def percent(self) -> float:
        return 100 * self.used / self.total if self.total else 0.0


@dataclass
class Process:
    pid: int
    user: str
    cpu: float
    mem: float
    command: str


@dataclass
class Container:
    name: str
    image: str
    status: str


@dataclass
class Stats:
    os_name: str = ""
    kernel: str = ""
    uptime_seconds: float = 0.0
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cpus: int = 1
    cpu_percent: float = 0.0
    mem_total: int = 0
    mem_available: int = 0
    swap_total: int = 0
    swap_free: int = 0
    disks: list[Disk] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    containers: list[Container] = field(default_factory=list)
    holdfast_version: str = ""

    @property
    def mem_percent(self) -> float:
        if not self.mem_total:
            return 0.0
        return 100 * (self.mem_total - self.mem_available) / self.mem_total


def _sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("##"):
            current = sections.setdefault(line[2:].strip(), [])
        elif current is not None and line.strip():
            current.append(line.rstrip())
    return sections


def _cpu_percent(lines: list[str]) -> float:
    """Busy share between the two `cpu` lines of /proc/stat, in percent."""
    if len(lines) < 2:
        return 0.0
    try:
        first, second = ([int(x) for x in line.split()[1:]] for line in lines[:2])
    except ValueError:
        return 0.0
    if len(first) < 5 or len(second) < 5:  # not a Linux /proc/stat
        return 0.0
    total = sum(second) - sum(first)
    # idle and iowait are fields 3 and 4: time spent not working.
    idle = (second[3] + second[4]) - (first[3] + first[4])
    return max(0.0, min(100.0, 100 * (total - idle) / total)) if total > 0 else 0.0


def parse_stats(text: str) -> Stats:
    s = _sections(text)
    stats = Stats()
    if s.get("os"):
        stats.os_name = s["os"][0]
        stats.kernel = s["os"][1] if len(s["os"]) > 1 else ""
    if s.get("uptime"):
        stats.uptime_seconds = float(s["uptime"][0])
    if s.get("load"):
        one, five, fifteen = (float(x) for x in s["load"][0].split()[:3])
        stats.load = (one, five, fifteen)
    if s.get("cpus"):
        stats.cpus = max(1, int(s["cpus"][0]))
    stats.cpu_percent = _cpu_percent(s.get("stat", []))

    meminfo = {}
    for line in s.get("mem", []):
        key, _, rest = line.partition(":")
        meminfo[key] = int(rest.split()[0]) * 1024  # kB in /proc/meminfo
    stats.mem_total = meminfo.get("MemTotal", 0)
    stats.mem_available = meminfo.get("MemAvailable", 0)
    stats.swap_total = meminfo.get("SwapTotal", 0)
    stats.swap_free = meminfo.get("SwapFree", 0)

    seen = set()
    for line in s.get("disk", []):
        parts = line.split()
        # filesystem 1B-blocks used available capacity mount
        if len(parts) < 6 or not parts[1].isdigit():
            continue
        mount = " ".join(parts[5:])
        if parts[0] in seen:  # the same device mounted twice (bind mounts)
            continue
        seen.add(parts[0])
        stats.disks.append(Disk(mount, int(parts[1]), int(parts[2])))

    for line in s.get("procs", []):
        parts = line.split(None, 4)
        if len(parts) == 5:
            stats.processes.append(
                Process(
                    int(parts[0]), parts[1], float(parts[2]), float(parts[3]), parts[4]
                )
            )

    for line in s.get("docker", []):
        if line.count("|") != 2:  # a "docker: not found" hint, not a container
            continue
        name, _, rest = line.partition("|")
        image, _, status = rest.partition("|")
        stats.containers.append(Container(name, image, status))

    if s.get("holdfast"):
        stats.holdfast_version = s["holdfast"][0].strip()
    return stats


def collect(conn: Connection) -> Stats:
    result = conn.run(f"bash -c {shlex.quote(COLLECT_SCRIPT)}", timeout=30)
    if "##end" not in result.out:
        raise SshError(
            f"the server did not return its state: {result.err.strip()[:200]}"
        )
    return parse_stats(result.out)


def human_bytes(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def human_uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч"
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
