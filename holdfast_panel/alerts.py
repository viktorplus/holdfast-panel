"""From a server's numbers and diagnosis to a few sentences that need attention."""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import FAIL, OK, WARN, Diagnosis
from .monitor import Stats

DISK_WARN, DISK_FAIL = 85, 95
MEM_WARN = 92
LOAD_PER_CPU_WARN = 2.0


@dataclass(frozen=True)
class Alert:
    level: str
    text: str


def evaluate(
    stats: Stats | None, diagnosis: Diagnosis | None, reachable: bool = True
) -> list[Alert]:
    if not reachable:
        return [Alert(FAIL, "сервер недоступен")]
    alerts: list[Alert] = []
    if stats is not None:
        for disk in stats.disks:
            if disk.percent >= DISK_FAIL:
                alerts.append(
                    Alert(FAIL, f"диск {disk.mount} заполнен на {disk.percent:.0f}%")
                )
            elif disk.percent >= DISK_WARN:
                alerts.append(
                    Alert(WARN, f"диск {disk.mount} заполнен на {disk.percent:.0f}%")
                )
        if stats.mem_percent >= MEM_WARN:
            alerts.append(Alert(WARN, f"память занята на {stats.mem_percent:.0f}%"))
        if stats.load[0] / stats.cpus >= LOAD_PER_CPU_WARN:
            alerts.append(
                Alert(
                    WARN, f"высокая нагрузка: {stats.load[0]:.1f} на {stats.cpus} ядер"
                )
            )
    if diagnosis is not None:
        for item in diagnosis.items:
            if item.status in (FAIL, WARN):
                alerts.append(Alert(item.status, f"{item.name}: {item.detail}"))
    return alerts


def worst(alerts: list[Alert]) -> str:
    if any(a.level == FAIL for a in alerts):
        return FAIL
    return WARN if alerts else OK
