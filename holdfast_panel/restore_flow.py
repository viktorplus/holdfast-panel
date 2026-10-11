"""Restoring through `holdfast restore`, with the key present only while it is used.

The private key lives on this computer. It is written to a closed file on the
server for one step at a time and removed in a `finally`, whatever happens: a
failed step, a dropped connection, even a failure while removing it (that is
reported with the file's name, so it can be removed by hand). It is *not* on the
server while a person reads the plan, which may take an hour: the key goes up
for the check and the plan, comes down, and goes up again only after "yes".

A restore overwrites live data, so nothing is written before `confirm` has seen
the plan and said yes.
"""

from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .backups import BackupError, Component, SnapshotInfo
from .ssh import Connection

_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass
class Outcome:
    ok: bool
    stage: str
    message: str = ""
    key_removed: bool = True


@dataclass
class KeyOnServer:
    """Where the key is while a step runs, and whether it was removed afterwards."""

    path: str
    removed: bool = False


def _read_key(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read the key {path}: {exc}") from exc
    if not data.strip():
        raise BackupError(f"{path} is empty")
    return data


def _remove_key(conn: Connection, remote: str) -> bool:
    """Shred the key file; one retry; never raises (it runs inside `finally`)."""
    quoted = shlex.quote(remote)
    for _ in range(2):
        try:
            conn.run(f"shred -u {quoted} 2>/dev/null || rm -f {quoted}")
            if conn.run(f"test ! -e {quoted}").ok:
                return True
        except Exception:  # noqa: BLE001, S110 - the original problem matters more
            pass
    return False


@contextmanager
def server_key(conn: Connection, key_file: Path) -> Iterator[KeyOnServer]:
    """The key on the server for the length of a `with` block.

    The file is created before anything can go wrong with it, the removal is in
    a `finally` that cannot raise over the real error, and the yielded object
    says afterwards whether it is gone.
    """
    data = _read_key(key_file)
    state = KeyOnServer(f"/root/.holdfast-restore-key-{secrets.token_hex(8)}")
    try:
        conn.put_bytes(state.path, data)  # inside: a half-written key is removed too
        yield state
    finally:
        state.removed = _remove_key(conn, state.path)


def _leftover(state: KeyOnServer | None) -> str:
    if state is None or state.removed:
        return ""
    return f" The key file could not be removed from the server: {state.path}"


def verify_snapshot(
    conn: Connection,
    snapshot_dir: str,
    key_file: Path,
    on_text: Callable[[str], None] = lambda text: None,
) -> bool:
    """Read every artifact of a snapshot back through the key. Changes nothing."""
    with server_key(conn, key_file) as key:
        code = conn.run_stream(
            f"holdfast restore verify --snapshot {shlex.quote(snapshot_dir)} "
            f"--identity {shlex.quote(key.path)}",
            on_text,
            timeout=3600,
        )
    return code == 0


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
        if not _COMPONENT.fullmatch(component):
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
    flags: list[str] = []
    if from_other_host:
        flags.append("--from-other-host")
    if not stop_containers:
        flags.append("--no-stop")

    # Phase 1, key up: is the snapshot readable, and what would be done.
    with server_key(conn, key_file) as plan_key:
        on_stage("Проверка снимка")
        verify = conn.run_stream(
            f"holdfast restore verify --snapshot {shlex.quote(snapshot_dir)} "
            f"--identity {shlex.quote(plan_key.path)}",
            on_text,
            timeout=3600,
        )
        chunks: list[str] = []
        plan_code = -1
        if verify == 0:
            on_stage("План восстановления")
            plan_code = conn.run_stream(
                _restore_command(
                    snapshot_dir,
                    plan_key.path,
                    component=component,
                    extra=(*flags, "--dry-run"),
                ),
                chunks.append,
                timeout=600,
            )
    # Phase 1 is over and the key is off the server again.
    if verify != 0:
        return _done(
            Outcome(
                False, "verify", "the snapshot does not read back; nothing was changed"
            ),
            plan_key,
        )
    plan = "".join(chunks)
    if plan_code != 0:
        on_text(plan)
        return _done(
            Outcome(False, "plan", "the plan could not be made; nothing was changed"),
            plan_key,
        )
    if not confirm(plan):  # nothing is on the server while the person decides
        return _done(
            Outcome(False, "confirm", "cancelled; nothing was changed"), plan_key
        )

    # Phase 2, key up again, only for the restore.
    with server_key(conn, key_file) as run_key:
        on_stage("Восстановление")
        code = conn.run_stream(
            _restore_command(
                snapshot_dir,
                run_key.path,
                component=component,
                extra=(*flags, "--yes"),
            ),
            on_text,
            timeout=24 * 3600,
        )
    if code != 0:
        return _done(
            Outcome(
                False,
                "restore",
                "the restore stopped half way; the machine may be PARTIALLY "
                "RESTORED. Fix the cause and run it again: the steps can be repeated.",
            ),
            run_key,
        )
    return _done(Outcome(True, "done", "restored"), run_key)


def _done(outcome: Outcome, key: KeyOnServer | None) -> Outcome:
    outcome.key_removed = key is None or key.removed
    outcome.message += _leftover(key)
    return outcome


def _project_components(info: SnapshotInfo) -> list[Component]:
    seen: set[str] = set()
    projects = []
    for c in info.components:
        if c.name.startswith("project-") and c.name not in seen:
            seen.add(c.name)
            projects.append(c)
    return projects


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
    projects = _project_components(info)
    if not projects:
        return Outcome(
            False,
            "plan",
            "this snapshot holds no project folder (a component named project-…), "
            "so there is nothing to start containers from",
        )
    unknown = [p.name for p in projects if not p.target]
    if unknown:
        return Outcome(
            False,
            "plan",
            f"the snapshot does not say which folder {', '.join(unknown)} came "
            "from, so it cannot be started on its own; restore it with the "
            "ordinary restore and start the containers by hand",
        )
    steps = [
        f"1. вернуть папки проектов: {', '.join(c.target for c in projects)}",
        "2. запустить их: docker compose up -d (в каждой папке)",
        "3. восстановить базы и тома из снимка",
    ]
    summary = (
        f"Снимок {info.name} с машины {info.host_label or '?'}.\n"
        + "\n".join(steps)
        + "\nДанные на этом сервере будут перезаписаны."
    )
    flags = ("--from-other-host",) if from_other_host else ()

    # The snapshot is checked first, while nothing has been changed.
    with server_key(conn, key_file) as check_key:
        on_stage("Проверка снимка")
        verify = conn.run_stream(
            f"holdfast restore verify --snapshot {shlex.quote(snapshot_dir)} "
            f"--identity {shlex.quote(check_key.path)}",
            on_text,
            timeout=3600,
        )
    if verify != 0:
        return _done(
            Outcome(
                False, "verify", "the snapshot does not read back; nothing was changed"
            ),
            check_key,
        )
    if not confirm(summary):
        return _done(
            Outcome(False, "confirm", "cancelled; nothing was changed"), check_key
        )

    with server_key(conn, key_file) as run_key:
        outcome = _recover_steps(
            conn, snapshot_dir, run_key.path, projects, flags, on_text, on_stage
        )
    return _done(outcome, run_key)


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
        on_stage(f"Запуск {project.target}")
        folder = shlex.quote(project.target)
        # No `-p`: compose names the project from the folder, as it did when the
        # volumes the snapshot refers to were created. Forcing another name would
        # make new, empty volumes next to the ones holdfast restores into.
        if (
            conn.run_stream(
                f"cd {folder} && docker compose up -d", on_text, timeout=1800
            )
            != 0
        ):
            return Outcome(
                False, "compose", f"docker compose failed in {project.target}"
            )
        # Databases need a moment before a dump can be loaded; holdfast itself
        # waits for the containers it restores into, so only "something is up"
        # is checked here.
        wait = (
            f"cd {folder} && for i in $(seq 1 30); do "
            '[ -n "$(docker compose ps --status running -q)" ] && exit 0; sleep 2; done; exit 1'
        )
        if conn.run_stream(wait, on_text, timeout=120) != 0:
            return Outcome(
                False, "compose", f"the containers in {project.target} did not start"
            )

    on_stage("Базы и тома")
    full = _restore_command(snapshot_dir, key, extra=(*flags, "--yes"))
    if conn.run_stream(full, on_text, timeout=24 * 3600) != 0:
        return Outcome(
            False,
            "restore",
            "the full restore stopped half way; run it again once the cause is fixed",
        )
    return Outcome(True, "done", "restored")
