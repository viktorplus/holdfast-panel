"""Restoring through `holdfast restore`, with the key present only for the run.

The private key lives on this computer. For a restore it is written to a
closed file on the server, used, and removed in a `finally`: whatever happens
in between, the server is not left holding it. A restore overwrites live data,
so nothing is written before `confirm` has seen the plan and said yes.
"""

from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .backups import BackupError, SnapshotInfo
from .ssh import Connection

_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class Outcome:
    ok: bool
    stage: str
    message: str = ""
    key_removed: bool = True


def _read_key(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read the key {path}: {exc}") from exc
    if not data.strip():
        raise BackupError(f"{path} is empty")
    return data


@contextmanager
def server_key(conn: Connection, key_file: Path) -> Iterator[tuple[str, list[bool]]]:
    """The key on the server for the length of a `with` block.

    Yields (remote path, flag list). On exit the file is shredded; the flag
    list gets True appended if that worked, so callers can report a leftover.
    """
    remote = f"/root/.holdfast-restore-key-{secrets.token_hex(8)}"
    removed: list[bool] = []
    conn.put_bytes(remote, _read_key(key_file))
    try:
        yield remote, removed
    finally:
        cleanup = conn.run(
            f"shred -u {shlex.quote(remote)} 2>/dev/null || rm -f {shlex.quote(remote)}"
        )
        gone = conn.run(f"test ! -e {shlex.quote(remote)}")
        removed.append(cleanup.ok and gone.ok)


def _restore_command(
    snapshot_dir: str,
    key: str,
    *,
    component: str | None = None,
    extra: tuple[str, ...] = (),
) -> str:
    parts = [
        "holdfast restore",
        f"--snapshot {shlex.quote(snapshot_dir)}",
        f"--identity {shlex.quote(key)}",
    ]
    if component is not None:
        if not _COMPONENT.match(component):
            raise BackupError(f"{component!r} is not a component name")
        parts.append(f"--component {component}")
    return " ".join([*parts, *extra])


def restore_snapshot(
    conn: Connection,
    snapshot_dir: str,
    key_file: Path,
    *,
    component: str | None = None,
    from_other_host: bool = False,
    stop_containers: bool = True,
    confirm: Callable[[str], bool],
    on_text: Callable[[str], None] = lambda text: None,
    on_stage: Callable[[str], None] = lambda stage: None,
) -> Outcome:
    """verify -> plan -> confirm -> restore, on the server the snapshot is on."""
    flags = []
    if from_other_host:
        flags.append("--from-other-host")
    if not stop_containers:
        flags.append("--no-stop")

    with server_key(conn, key_file) as (key, removed):
        outcome = _verify_plan_restore(
            conn, snapshot_dir, key, component, tuple(flags), confirm, on_text, on_stage
        )
    outcome.key_removed = removed[0] if removed else False
    if not outcome.key_removed:
        outcome.message += " The key file could not be removed from the server."
    return outcome


def _verify_plan_restore(
    conn, snapshot_dir, key, component, flags, confirm, on_text, on_stage
) -> Outcome:
    on_stage("Проверка снимка")
    verify = conn.run_stream(
        f"holdfast restore verify --snapshot {shlex.quote(snapshot_dir)} "
        f"--identity {shlex.quote(key)}",
        on_text,
        timeout=3600,
    )
    if verify != 0:
        return Outcome(
            False, "verify", "the snapshot does not read back; nothing was changed"
        )

    on_stage("План восстановления")
    chunks: list[str] = []
    plan_cmd = _restore_command(
        snapshot_dir, key, component=component, extra=(*flags, "--dry-run")
    )
    code = conn.run_stream(plan_cmd, chunks.append, timeout=600)
    plan = "".join(chunks)
    if code != 0:
        on_text(plan)
        return Outcome(False, "plan", "the plan could not be made; nothing was changed")
    if not confirm(plan):
        return Outcome(False, "confirm", "cancelled; nothing was changed")

    on_stage("Восстановление")
    real = _restore_command(
        snapshot_dir, key, component=component, extra=(*flags, "--yes")
    )
    code = conn.run_stream(real, on_text, timeout=24 * 3600)
    if code != 0:
        return Outcome(
            False,
            "restore",
            "the restore stopped half way; the machine may be PARTIALLY RESTORED. "
            "Fix the cause and run it again: the steps can be repeated.",
        )
    return Outcome(True, "done", "restored")


def recover_new_server(
    conn: Connection,
    snapshot_dir: str,
    info: SnapshotInfo,
    key_file: Path,
    *,
    from_other_host: bool = False,
    confirm: Callable[[str], bool],
    on_text: Callable[[str], None] = lambda text: None,
    on_stage: Callable[[str], None] = lambda stage: None,
) -> Outcome:
    """A new server from a snapshot: projects, then compose up, then everything.

    The order is the one in docs/ru/04-restore.md: a database dump and an
    anonymous volume can only be loaded into containers that already exist, and
    the containers need the project folder (compose file, .env) first.
    """
    projects = [c for c in info.components if c.name.startswith("project-")]
    if not projects:
        return Outcome(
            False,
            "plan",
            "this snapshot holds no project folder (a component named project-…), "
            "so there is nothing to start containers from",
        )
    steps = [
        f"1. вернуть папки проектов: {', '.join(c.target for c in projects)}",
        "2. запустить их: docker compose up -d",
        "3. восстановить базы и тома из снимка",
    ]
    summary = (
        f"Снимок {info.name} с машины {info.host_label or '?'}.\n"
        + "\n".join(steps)
        + "\nДанные на этом сервере будут перезаписаны."
    )
    if not confirm(summary):
        return Outcome(False, "confirm", "cancelled; nothing was changed")

    flags = ("--from-other-host",) if from_other_host else ()
    with server_key(conn, key_file) as (key, removed):
        outcome = _recover_steps(
            conn, snapshot_dir, key, projects, flags, on_text, on_stage
        )
    outcome.key_removed = removed[0] if removed else False
    if not outcome.key_removed:
        outcome.message += " The key file could not be removed from the server."
    return outcome


def _recover_steps(
    conn, snapshot_dir, key, projects, flags, on_text, on_stage
) -> Outcome:
    for project in projects:
        on_stage(f"Папка проекта {project.target}")
        cmd = _restore_command(
            snapshot_dir,
            key,
            component=project.name,
            extra=(*flags, "--no-stop", "--yes"),
        )
        if conn.run_stream(cmd, on_text, timeout=3600) != 0:
            return Outcome(False, "project", f"could not restore {project.target}")

    for project in projects:
        name = project.target.rstrip("/").rpartition("/")[2]
        if not _PROJECT_NAME.match(name):
            return Outcome(
                False, "compose", f"{project.target}: not a usable project name"
            )
        on_stage(f"Запуск {name}")
        up = (
            f"cd {shlex.quote(project.target)} && "
            f"docker compose -p {shlex.quote(name)} up -d"
        )
        if conn.run_stream(up, on_text, timeout=1800) != 0:
            return Outcome(
                False, "compose", f"docker compose failed in {project.target}"
            )
        # Databases need a moment before a dump can be loaded into them.
        wait = (
            f"cd {shlex.quote(project.target)} && for i in $(seq 1 30); do "
            f'[ -n "$(docker compose -p {shlex.quote(name)} ps --status running -q)" ] '
            "&& exit 0; sleep 2; done; exit 1"
        )
        if conn.run_stream(wait, on_text, timeout=120) != 0:
            return Outcome(False, "compose", f"the containers of {name} did not start")
        conn.run_stream("sleep 15", on_text, timeout=60)

    on_stage("Базы и тома")
    full = _restore_command(snapshot_dir, key, extra=(*flags, "--yes"))
    if conn.run_stream(full, on_text, timeout=24 * 3600) != 0:
        return Outcome(
            False,
            "restore",
            "the full restore stopped half way; run it again once the cause is fixed",
        )
    return Outcome(True, "done", "restored")
