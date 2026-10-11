"""Run one template on several servers, one after another.

A failure on one server is that server's result; it does not stop the others.
Dangerous templates are the caller's to confirm: the panel asks once, for the
whole list, before calling this.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import actionlog
from .ssh import Connection
from .store import Server
from .templates import RunResult, Template, run_template


@dataclass
class ServerResult:
    server: str
    ok: bool
    detail: str
    run: RunResult | None = None


def run_on_servers(
    servers: list[Server],
    template: Template,
    values: dict[str, str],
    open_connection: Callable[[Server], Connection],
    log_path: Path,
    on_event: Callable[[str, str, str], None] = lambda server, kind, text: None,
) -> list[ServerResult]:
    results = []
    for server in servers:
        on_event(server.name, "server", server.name)
        try:
            with open_connection(server) as conn:
                run = run_template(
                    conn,
                    template,
                    values,
                    lambda kind, text, s=server: on_event(s.name, kind, text),
                )
        except Exception as exc:  # noqa: BLE001 - one server's trouble is its own result
            result = ServerResult(server.name, False, str(exc) or type(exc).__name__)
        else:
            detail = (
                "выполнено" if run.ok else f"остановилось на шаге «{run.failed_step}»"
            )
            result = ServerResult(server.name, run.ok, detail, run)
        actionlog.record(
            log_path, server.name, f"шаблон {template.id}", result.ok, result.detail
        )
        results.append(result)
    return results
