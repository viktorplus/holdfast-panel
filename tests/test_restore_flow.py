import re
from pathlib import Path

import pytest

from holdfast_panel.backups import BackupError, Component, SnapshotInfo
from holdfast_panel.restore_flow import recover_new_server, restore_snapshot
from holdfast_panel.ssh import Result, SshError

SNAP = "/opt/backups/20260920-231500"


class Server:
    """A stand-in for Connection that records, and knows whether the key file
    is on the 'server' at the moment each command runs."""

    def __init__(self, fail=None, raise_on=None, keep_key=False):
        self.log = []  # (command, key_present)
        self.files = {}
        self.fail = fail or {}
        self.raise_on = raise_on
        self.keep_key = keep_key

    def put_bytes(self, path, data, mode="600"):
        self.files[path] = data

    def _record(self, command):
        self.log.append((command, bool(self.files)))

    def run(self, command, timeout=60):
        self._record(command)
        match = re.search(r"(?:shred -u|rm -f) (\S+)", command)
        if match and not self.keep_key:
            self.files.pop(match.group(1), None)
        if command.startswith("test ! -e"):
            return Result(0 if not self.files else 1, "", "")
        return Result(0, "", "")

    def run_stream(self, command, on_text, timeout=0):
        self._record(command)
        if self.raise_on and self.raise_on in command:
            raise SshError("connection lost")
        on_text(f"[{command[:30]}]\n")
        for needle, code in self.fail.items():
            if needle in command:
                return code
        return 0

    def commands(self):
        return [c for c, _ in self.log]


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("AGE-SECRET-KEY-1TESTKEY\n")
    return path


def streamed(server):
    return [c for c in server.commands() if c.startswith(("holdfast", "cd ", "sleep"))]


# ---- same server -------------------------------------------------------------


def test_verify_then_plan_then_confirm_then_restore_in_that_order(key):
    server = Server()
    seen = {}

    def confirm(plan):
        seen["plan"] = plan
        return True

    outcome = restore_snapshot(server, SNAP, key, confirm=confirm)

    assert outcome.ok and outcome.key_removed
    verify, plan, real = streamed(server)
    assert verify.startswith("holdfast restore verify")
    assert plan.endswith("--dry-run") and "--yes" not in plan
    assert real.endswith("--yes") and "--dry-run" not in real
    assert "holdfast restore" in seen["plan"]  # the dry-run text reached the user
    assert server.files == {}


def test_the_key_is_on_the_server_only_while_commands_need_it(key):
    server = Server()
    restore_snapshot(server, SNAP, key, confirm=lambda plan: True)

    streamed_with_key = [
        present for c, present in server.log if c.startswith("holdfast")
    ]
    assert streamed_with_key and all(streamed_with_key)
    assert server.files == {}
    assert any(c.startswith("shred -u") for c in server.commands())


def test_a_snapshot_that_does_not_verify_is_never_planned_or_restored(key):
    server = Server(fail={"restore verify": 1})
    outcome = restore_snapshot(
        server, SNAP, key, confirm=lambda plan: pytest.fail("asked")
    )

    assert not outcome.ok and outcome.stage == "verify"
    assert len(streamed(server)) == 1
    assert server.files == {}


def test_declining_the_plan_changes_nothing_and_removes_the_key(key):
    server = Server()
    outcome = restore_snapshot(server, SNAP, key, confirm=lambda plan: False)

    assert not outcome.ok and outcome.stage == "confirm"
    assert not any("--yes" in c for c in server.commands())
    assert server.files == {}


def test_a_restore_that_stops_half_way_says_so_and_still_removes_the_key(key):
    server = Server(fail={"--yes": 1})
    outcome = restore_snapshot(server, SNAP, key, confirm=lambda plan: True)

    assert not outcome.ok and outcome.stage == "restore"
    assert "PARTIALLY RESTORED" in outcome.message
    assert server.files == {}


def test_a_dropped_connection_still_removes_the_key(key):
    server = Server(raise_on="--yes")
    with pytest.raises(SshError):
        restore_snapshot(server, SNAP, key, confirm=lambda plan: True)
    assert server.files == {}
    assert any(c.startswith("shred -u") for c in server.commands())


def test_a_key_that_cannot_be_removed_is_reported(key):
    server = Server(keep_key=True)
    outcome = restore_snapshot(server, SNAP, key, confirm=lambda plan: True)

    assert outcome.ok and not outcome.key_removed
    assert "could not be removed" in outcome.message


def test_flags_for_component_other_host_and_containers(key):
    server = Server()
    restore_snapshot(
        server,
        SNAP,
        key,
        component="myapp-db-1",
        from_other_host=True,
        stop_containers=False,
        confirm=lambda plan: True,
    )
    real = streamed(server)[-1]
    for flag in ("--component myapp-db-1", "--from-other-host", "--no-stop", "--yes"):
        assert flag in real


def test_a_hostile_component_name_is_refused_before_anything_runs(key):
    server = Server()
    with pytest.raises(BackupError):
        restore_snapshot(
            server, SNAP, key, component="x; reboot", confirm=lambda p: True
        )
    assert not any("reboot" in c for c in server.commands())


def test_a_missing_or_empty_key_file_stops_before_any_command(tmp_path):
    server = Server()
    with pytest.raises(BackupError, match="cannot read"):
        restore_snapshot(server, SNAP, tmp_path / "nope.txt", confirm=lambda p: True)
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n")
    with pytest.raises(BackupError, match="empty"):
        restore_snapshot(server, SNAP, empty, confirm=lambda p: True)
    assert server.log == [] and server.files == {}


# ---- a new server ------------------------------------------------------------


def info(*names):
    return SnapshotInfo(
        name="20260920-231500",
        location=SNAP,
        host_label="web-1",
        components=tuple(
            Component(n, "path", 10, f"/root/{n.removeprefix('project-')}")
            for n in names
        ),
    )


def test_a_new_server_gets_projects_then_compose_then_the_rest(key):
    server = Server()
    asked = []
    outcome = recover_new_server(
        server,
        SNAP,
        info("project-myapp"),
        key,
        from_other_host=True,
        confirm=lambda text: asked.append(text) or True,
    )

    assert outcome.ok and outcome.key_removed and server.files == {}
    cmds = streamed(server)
    project, up, wait, _sleep, full = cmds
    assert "--component project-myapp" in project and "--no-stop" in project
    assert "--from-other-host" in project and "--yes" in project
    assert up == "cd /root/myapp && docker compose -p myapp up -d"
    assert "docker compose -p myapp ps --status running -q" in wait
    assert "--component" not in full and full.endswith("--yes")
    assert "--from-other-host" in full
    assert "/root/myapp" in asked[0] and "перезаписаны" in asked[0]


def test_declining_the_new_server_summary_touches_nothing_not_even_the_key(key):
    server = Server()
    outcome = recover_new_server(
        server, SNAP, info("project-myapp"), key, confirm=lambda t: False
    )

    assert not outcome.ok and outcome.stage == "confirm"
    assert server.log == []


def test_a_snapshot_without_a_project_cannot_start_a_new_server(key):
    server = Server()
    outcome = recover_new_server(
        server, SNAP, info("myapp-db-1"), key, confirm=lambda t: True
    )

    assert not outcome.ok and "project" in outcome.message
    assert server.log == []


def test_compose_failing_stops_before_the_databases_are_loaded(key):
    server = Server(fail={"docker compose -p myapp up": 1})
    outcome = recover_new_server(
        server, SNAP, info("project-myapp"), key, confirm=lambda t: True
    )

    assert not outcome.ok and outcome.stage == "compose"
    assert not any(
        c.endswith("--yes") and "--component" not in c for c in streamed(server)
    )
    assert server.files == {}


def test_a_project_that_will_not_restore_stops_everything(key):
    server = Server(fail={"--component project-myapp": 1})
    outcome = recover_new_server(
        server, SNAP, info("project-myapp"), key, confirm=lambda t: True
    )

    assert not outcome.ok and outcome.stage == "project"
    assert not any("docker compose" in c for c in server.commands())
    assert server.files == {}


def test_the_key_file_name_is_not_reused(key):
    first, second = Server(), Server()
    restore_snapshot(first, SNAP, key, confirm=lambda p: True)
    restore_snapshot(second, SNAP, key, confirm=lambda p: True)
    name = lambda s: re.search(r"--identity (\S+)", s.commands()[1]).group(1)
    assert name(first) != name(second)
    assert Path(name(first)).name.startswith(".holdfast-restore-key-")
