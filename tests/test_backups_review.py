"""Findings of the review of snapshot handling, restore flows and the config editor.

Each test fails on the code before its fix."""

import json
import re
from pathlib import Path

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash

from holdfast_panel import backups
from holdfast_panel.backups import (
    BackupError,
    Component,
    SnapshotInfo,
    list_remote,
    list_server,
    parse_manifest,
    pull_from_remote,
    push_from_pc,
)
from holdfast_panel.config_edit import (
    ConfigEditError,
    save_config,
    set_backup_mode,
    set_excludes,
)
from holdfast_panel.restore_flow import (
    recover_new_server,
    restore_snapshot,
    server_key,
)
from holdfast_panel.ssh import Result, SshError, connect, fetch_host_fingerprint
from holdfast_panel.store import Server, Storage, StorageStore, StoreError

SNAPSHOT = Path(__file__).parent / "fixtures" / "pc" / "web-1" / "20260920-231500"
needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs a bash with HOME")
SNAP = "/opt/backups/20260920-231500"


# ---- the folder of a project -----------------------------------------------------------------


def test_the_project_folder_comes_from_components_not_from_the_recipe():
    manifest = (SNAPSHOT / "manifest.json").read_text("utf-8")
    info = parse_manifest(manifest, "/x")

    # holdfast writes every path recipe as target "/": that is not a project folder
    assert json.loads(manifest)["artifacts"][0]["restore"]["target"] == "/"
    assert info.project_dirs == ["/root/myapp"]
    assert info.components[0].target == "/root/myapp"
    assert [c.target for c in info.components[1:]] == ["", ""]


def test_a_snapshot_that_does_not_say_where_a_project_lived_is_not_started(tmp_path):
    key = tmp_path / "k"
    key.write_text("AGE-SECRET-KEY-1X")
    info = SnapshotInfo(
        name="20260920-231500",
        location=SNAP,
        components=(Component("project-myapp", "path", 10, ""),),
    )
    conn = KeyServer()

    outcome = recover_new_server(conn, SNAP, info, key, confirm=lambda t: True)

    assert not outcome.ok and "does not say which folder" in outcome.message
    assert conn.log == []  # nothing, not even the key, was sent


# ---- the key ------------------------------------------------------------------------------------


class KeyServer:
    """Records everything, and knows when the key file exists on the 'server'."""

    def __init__(self, fail=None, put_error=None, remove_error=False, keep=False):
        self.log, self.files = [], {}
        self.fail, self.put_error = fail or {}, put_error
        self.remove_error, self.keep = remove_error, keep
        self.key_present_at = []  # for each streamed command: was a key file there?

    def put_bytes(self, path, data, mode="600"):
        self.files[path] = data
        if self.put_error:
            raise self.put_error  # the file exists, then the transfer fails

    def run(self, command, timeout=60):
        self.log.append(command)
        m = re.search(r"(?:shred -u|rm -f) (\S+)", command)
        if m:
            if self.remove_error:
                raise SshError("connection lost while removing")
            if not self.keep:
                self.files.pop(m.group(1), None)
        if command.startswith("test ! -e"):
            return Result(0 if not self.files else 1, "", "")
        return Result(0, "", "")

    def run_stream(self, command, on_text, timeout=0):
        self.log.append(command)
        self.key_present_at.append((command, bool(self.files)))
        on_text("plan text\n")
        return next((c for n, c in self.fail.items() if n in command), 0)


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("AGE-SECRET-KEY-1TESTKEY\n")
    return path


def test_the_key_is_not_on_the_server_while_the_person_reads_the_plan(key):
    conn = KeyServer()
    during_confirm = []

    def confirm(plan):
        during_confirm.append(dict(conn.files))  # what is on the server right now
        return True

    outcome = restore_snapshot(conn, SNAP, key, confirm=confirm)

    assert outcome.ok and during_confirm == [{}]
    present = dict(conn.key_present_at)
    assert all(v for c, v in present.items() if "restore" in c)  # up when it is used
    assert conn.files == {}


def test_a_key_that_was_half_written_is_still_removed(key):
    conn = KeyServer(put_error=SshError("could not write: connection dropped"))

    with pytest.raises(SshError, match="dropped"), server_key(conn, key):
        pytest.fail("the body must not run")

    assert conn.files == {}  # the file that did get created is gone


def test_a_failing_cleanup_does_not_hide_the_real_error_and_names_the_file(key):
    conn = KeyServer(fail={"--yes": 1}, remove_error=True)
    conn.files.clear()

    outcome = restore_snapshot(conn, SNAP, key, confirm=lambda p: True)

    assert not outcome.ok and outcome.stage == "restore"  # the real problem
    assert not outcome.key_removed
    assert (
        "/root/.holdfast-restore-key-" in outcome.message
    )  # so it can be removed by hand


def test_an_error_in_the_body_is_not_replaced_by_a_cleanup_error(key):
    conn = KeyServer(remove_error=True)
    with pytest.raises(ZeroDivisionError), server_key(conn, key):
        raise ZeroDivisionError


def test_the_cleanup_is_tried_twice(key):
    class Flaky(KeyServer):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def run(self, command, timeout=60):
            if command.startswith("shred"):
                self.attempts += 1
                if self.attempts == 1:
                    raise SshError("hiccup")
            return super().run(command, timeout)

    conn = Flaky()
    with server_key(conn, key) as state:
        pass
    assert state.removed and conn.files == {} and conn.attempts == 2


# ---- the compose project name --------------------------------------------------------------------------


def test_a_new_server_is_started_without_forcing_a_compose_project_name(key):
    conn = KeyServer()
    info = SnapshotInfo(
        name="20260920-231500",
        location=SNAP,
        components=(Component("project-myapp", "path", 10, "/srv/My App"),),
    )

    outcome = recover_new_server(conn, SNAP, info, key, confirm=lambda t: True)

    assert outcome.ok
    up = next(c for c, _ in conn.key_present_at if "docker compose up" in c)
    assert up == "cd '/srv/My App' && docker compose up -d"  # quoted, and no -p


# ---- partial copies ------------------------------------------------------------------------------------------


class Recording:
    def __init__(self, fail_at=None):
        self.sent, self.fail_at = [], fail_at

    def run(self, command, timeout=60):
        self.sent.append(command)
        return Result(0, "", "")

    def run_stream(self, command, on_text, timeout=0):
        self.sent.append(command)
        return 1 if self.fail_at and self.fail_at in command else 0


def test_a_failed_copy_from_a_remote_leaves_no_partial_snapshot_behind():
    conn = Recording(fail_at="sha256sum")
    code = pull_from_remote(
        conn, "r:b", "web-1", "20260920-231500", "/opt/backups", lambda t: None
    )

    assert code == 1
    assert conn.sent[-1] == "rm -rf /opt/backups/.20260920-231500.part"
    assert not any("mv " in c for c in conn.sent)  # never moved into place


def test_pulling_onto_an_existing_snapshot_is_refused_by_the_guard():
    conn = Recording(fail_at="exit 17")
    code = pull_from_remote(
        conn, "r:b", "web-1", "20260920-231500", "/opt/backups", lambda t: None
    )
    assert code == 1 and not any(c.startswith("rclone copy") for c in conn.sent)


@pytest.fixture
def conn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sshd = FakeSshd(home)
    server = Server(
        name="t",
        host="127.0.0.1",
        port=sshd.port,
        host_fingerprint=fetch_host_fingerprint("127.0.0.1", sshd.port),
    )
    session = connect(server, password=PASSWORD)
    yield session
    session.close()
    sshd.close()


@needs_bash
def test_an_upload_appears_under_its_name_only_when_complete(conn, tmp_path):
    import uuid

    root = f"/tmp/hf-root-{uuid.uuid4().hex}"
    try:
        final = push_from_pc(conn, SNAPSHOT, root)
        names = conn.run(f"ls -A {root}").out.split()
        assert names == ["20260920-231500"]  # no .incoming-... left behind
        assert final == f"{root}/20260920-231500"
    finally:
        conn.run(f"rm -rf {root}")


@needs_bash
def test_a_failing_upload_leaves_no_staging_folder(conn, tmp_path, monkeypatch):
    import uuid

    root = f"/tmp/hf-root-{uuid.uuid4().hex}"
    real = conn.upload_dir

    def broken(local, parent, on_bytes=lambda n: None):
        real(local, parent, on_bytes)
        raise SshError("link dropped at the very end")

    monkeypatch.setattr(conn, "upload_dir", broken)
    try:
        with pytest.raises(SshError):
            push_from_pc(conn, SNAPSHOT, root)
        assert conn.run(f"ls -A {root}").out.split() == []
    finally:
        conn.run(f"rm -rf {root}")


# ---- listings that cannot be told from "nothing there" ----------------------------------------------------------------


def test_a_missing_snapshot_folder_is_an_error_not_an_empty_list():
    class NoFolder(Recording):
        def run(self, command, timeout=60):
            self.sent.append(command)
            return Result(1 if command.startswith("test -d") else 0, "", "")

    with pytest.raises(BackupError, match="нет папки"):
        list_server(NoFolder(), "/opt/backups")


def test_rclone_missing_or_refusing_is_an_error_not_an_empty_list():
    class NoRclone(Recording):
        def run(self, command, timeout=60):
            return Result(1 if "command -v rclone" in command else 0, "", "")

    with pytest.raises(BackupError, match="не установлен rclone"):
        list_remote(NoRclone(), "shared:b", "web-1")

    class Refused(Recording):
        def run(self, command, timeout=60):
            if command.startswith("rclone lsf"):
                return Result(3, "", "directory not found")
            return Result(0, "", "")

    with pytest.raises(BackupError, match="directory not found"):
        list_remote(Refused(), "shared:b", "web-1")


def test_an_empty_remote_is_an_empty_list():
    class Empty(Recording):
        def run(self, command, timeout=60):
            return Result(0, "", "")

    assert list_remote(Empty(), "shared:b", "web-1").snapshots == []


def test_rclone_output_is_plain_lines_not_a_progress_bar():
    conn = Recording()
    backups.push_to_remote(
        conn, "/opt/backups/20260920-231500", "r:b", "web-1", lambda t: None
    )
    assert "--progress" not in conn.sent[0] and "--stats-one-line" in conn.sent[0]


# ---- storages ------------------------------------------------------------------------------------------------------------------


def test_a_remote_location_with_a_space_or_a_quote_is_refused_when_it_is_saved(
    tmp_path,
):
    store = StorageStore(tmp_path / "s.json")
    for bad in ("shared:my backups", "shared:'x'", "shared:a;b", "nocolon"):
        with pytest.raises(StoreError):
            store.add(Storage(name=f"n-{bad}", kind="remote", location=bad))
    store.add(Storage(name="ok", kind="remote", location="shared:backups/web"))


# ---- the config editor ---------------------------------------------------------------------------------------------------------------


BASE = 'host_label = "web-1"\n\n[backup]\nmode = "auto"\n'


class ConfigServer:
    def __init__(self, text=BASE, dry=(0, 0), fail_restore=False, mv_fails=False):
        self.files = {"/etc/holdfast/holdfast.toml": text}
        self.dry, self.log = list(dry), []
        self.fail_restore, self.mv_fails = fail_restore, mv_fails
        self.n = 0

    def run(self, command, timeout=60):
        self.log.append(command)
        if command == "holdfast backup --dry-run":
            code = self.dry.pop(0)
            return Result(code, "plan\n" if code == 0 else "", "boom" if code else "")
        if command.startswith("date"):
            self.n += 1
            return Result(0, f"20261011-120000-{self.n:09d}\n", "")
        if command.startswith("cat "):
            return Result(0, self.files["/etc/holdfast/holdfast.toml"], "")
        for verb in ("cp -p ", "mv -f "):
            if command.startswith(verb):
                if verb == "mv -f " and self.mv_fails:
                    return Result(1, "", "mv failed")
                src, dst = (p.strip("'") for p in command[len(verb) :].split())
                if (
                    verb == "cp -p "
                    and self.fail_restore
                    and src.endswith(tuple("0123456789"))
                    and dst.endswith(".toml")
                ):
                    return Result(1, "", "cp failed")
                self.files[dst] = self.files[src]
                if verb == "mv -f ":
                    del self.files[src]
        return Result(0, "", "")

    def put_bytes(self, path, data, mode="600"):
        self.files[path] = data.decode()


def test_a_file_changed_on_the_server_since_it_was_loaded_is_not_overwritten():
    conn = ConfigServer(text=BASE + "# somebody else was here\n")
    with pytest.raises(ConfigEditError, match="changed since"):
        save_config(conn, set_backup_mode(BASE, "manual"), expected=BASE)
    assert conn.files["/etc/holdfast/holdfast.toml"].endswith(
        "somebody else was here\n"
    )
    assert not any("cp -p" in c for c in conn.log)


def test_two_saves_in_the_same_second_keep_two_different_copies():
    conn = ConfigServer(dry=[0, 0, 0, 0])
    first = save_config(conn, set_backup_mode(BASE, "manual"))
    second = save_config(conn, set_backup_mode(BASE, "auto"))
    assert first.backup_path != second.backup_path
    assert conn.files[first.backup_path] == BASE


def test_the_new_file_keeps_the_mode_and_owner_of_the_old_one():
    conn = ConfigServer()
    result = save_config(conn, set_backup_mode(BASE, "manual"))
    staged = next(
        i for i, c in enumerate(conn.log) if c.startswith("chmod --reference")
    )
    moved = next(i for i, c in enumerate(conn.log) if c.startswith("mv -f"))
    assert any(c.startswith("chown --reference") for c in conn.log)
    assert staged < moved and result.ok


def test_a_rollback_that_fails_is_not_reported_as_a_rollback():
    conn = ConfigServer(dry=[0, 1], fail_restore=True)
    result = save_config(conn, set_backup_mode(BASE, "manual"))
    assert result.rollback_failed and not result.rolled_back and not result.ok
    assert result.backup_path in result.message  # where the original is


def test_a_replace_that_fails_leaves_the_old_file_and_no_staging_file():
    conn = ConfigServer(mv_fails=True)
    with pytest.raises(ConfigEditError, match="old file is untouched"):
        save_config(conn, set_backup_mode(BASE, "manual"))
    assert conn.files["/etc/holdfast/holdfast.toml"] == BASE
    assert any(c.startswith("rm -f") for c in conn.log)


def test_windows_line_endings_are_kept():
    text = 'host_label = "web-1"\r\n\r\n[backup]\r\nmode = "auto"\r\n'
    new = set_backup_mode(text, "manual")
    assert new == 'host_label = "web-1"\r\n\r\n[backup]\r\nmode = "manual"\r\n'
    assert "\n" not in new.replace("\r\n", "")
    assert set_excludes(text, ["volume:x"]).count("\r\n") == set_excludes(
        text, ["volume:x"]
    ).count("\n")


def test_a_file_that_is_not_valid_toml_is_refused_not_guessed_at():
    with pytest.raises(ConfigEditError, match="not valid TOML"):
        set_backup_mode("mode = \n", "manual")
