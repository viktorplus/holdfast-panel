import json
import shutil
import uuid
from pathlib import Path

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash

from holdfast_panel import backups
from holdfast_panel.backups import (
    BackupError,
    list_pc,
    list_server,
    parse_manifest,
    pull_to_pc,
    push_from_pc,
    valid_label,
    valid_snapshot_name,
    valid_spec,
)
from holdfast_panel.ssh import Result, connect, fetch_host_fingerprint
from holdfast_panel.store import Server

FIXTURES = Path(__file__).parent / "fixtures" / "pc"
SNAPSHOT = FIXTURES / "web-1" / "20260920-231500"

needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs a bash with HOME")


# ---- reading a manifest ------------------------------------------------------


def test_a_manifest_written_by_holdfast_is_understood():
    info = parse_manifest((SNAPSHOT / "manifest.json").read_text("utf-8"), "/x/snap")

    assert info.name == "20260920-231500"
    assert info.host_label == "web-1"
    assert info.created_at.startswith("2026-09-20")
    assert [(c.name, c.kind) for c in info.components] == [
        ("project-myapp", "path"),
        ("myapp-db-1", "mysql_database"),
        ("volume-wp", "docker_volume"),
    ]
    assert info.project_dirs == ["/root/myapp"]
    assert info.total_bytes == sum(c.size for c in info.components) > 0
    assert info.encrypted is False


def test_encryption_is_read_from_the_manifest():
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text("utf-8"))
    manifest["encryption"] = {"enabled": True, "tool": "age"}
    assert parse_manifest(json.dumps(manifest), "/x").encrypted is True


@pytest.mark.parametrize(
    "text", ["{nope", "[]", '{"format": 99, "artifacts": []}', '{"format": 1}']
)
def test_a_manifest_that_cannot_be_read_is_an_error_naming_the_place(text):
    with pytest.raises(BackupError, match="/x/snap"):
        parse_manifest(text, "/x/snap")


# ---- names that reach a shell ------------------------------------------------


@pytest.mark.parametrize("bad", ["", "../etc", "2026-09-20", "20260920-231500; reboot"])
def test_a_snapshot_name_must_be_a_timestamp(bad):
    with pytest.raises(BackupError):
        valid_snapshot_name(bad)


@pytest.mark.parametrize("bad", ["", "a b", "x;y", "-rf", "../x"])
def test_a_label_must_be_a_plain_name(bad):
    with pytest.raises(BackupError):
        valid_label(bad)


@pytest.mark.parametrize(
    "bad", ["nocolon", "r:a b", "r:x;y", "r:$(id)", "r:`id`", "r:a'b"]
)
def test_an_rclone_path_cannot_carry_shell_syntax(bad):
    with pytest.raises(BackupError):
        valid_spec(bad)


def test_good_names_pass():
    assert valid_snapshot_name("20260920-231500")
    assert valid_label("web-1.example")
    assert valid_spec("shared:backups/") == "shared:backups"


# ---- on this computer --------------------------------------------------------


def test_snapshots_on_this_computer_are_found_by_machine():
    listing = list_pc(FIXTURES)
    assert [s.name for s in listing.snapshots] == ["20260920-231500"]
    assert list_pc(FIXTURES, "web-1").snapshots == listing.snapshots
    assert list_pc(FIXTURES, "other").snapshots == []
    assert list_pc(FIXTURES / "missing").snapshots == []


def test_a_broken_snapshot_on_this_computer_is_a_problem_not_a_crash(tmp_path):
    bad = tmp_path / "web-1" / "20260101-000000"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{nope", encoding="utf-8")
    shutil.copytree(SNAPSHOT, tmp_path / "web-1" / SNAPSHOT.name)

    listing = list_pc(tmp_path)

    assert [s.name for s in listing.snapshots] == ["20260920-231500"]
    assert len(listing.problems) == 1


# ---- commands the panel sends ------------------------------------------------


class Recording:
    def __init__(self, code=0, out=""):
        self.sent = []
        self.code, self.out = code, out

    def run(self, command, timeout=60):
        self.sent.append(command)
        return Result(self.code, self.out, "")

    def run_stream(self, command, on_text, timeout=0):
        self.sent.append(command)
        on_text("progress")
        return self.code


def test_sending_to_a_remote_names_machine_and_snapshot_and_never_deletes():
    conn = Recording()
    code = backups.push_to_remote(
        conn, "/opt/backups/20260920-231500", "shared:backups", "web-1", lambda t: None
    )
    assert code == 0
    (sent,) = conn.sent
    assert sent.startswith(
        "rclone copy /opt/backups/20260920-231500 shared:backups/web-1/20260920-231500"
    )
    assert "sync" not in sent and "move" not in sent and "delete" not in sent


def test_bringing_a_snapshot_back_checks_its_checksums():
    conn = Recording()
    backups.pull_from_remote(
        conn, "shared:b", "web-1", "20260920-231500", "/opt/backups", lambda t: None
    )
    guard, copy, check, move = conn.sent
    assert "exit 17" in guard and "/opt/backups/.20260920-231500.part" in guard
    # copied into a hidden folder, never straight into the final name
    assert (
        "rclone copy shared:b/web-1/20260920-231500 /opt/backups/.20260920-231500.part"
        in copy
    )
    assert check.endswith("sha256sum -c SHA256SUMS")
    assert move == (
        "test ! -e /opt/backups/20260920-231500 && "
        "mv /opt/backups/.20260920-231500.part /opt/backups/20260920-231500"
    )


def test_a_failed_copy_is_not_checked_as_if_it_had_worked():
    conn = Recording(code=3)
    assert (
        backups.pull_from_remote(
            conn, "r:b", "w", "20260920-231500", "/o", lambda t: None
        )
        == 3
    )
    assert len(conn.sent) == 1


def test_a_hostile_name_never_reaches_the_command_line():
    conn = Recording()
    with pytest.raises(BackupError):
        backups.push_to_remote(
            conn, "/opt/backups/x; reboot", "r:b", "web-1", lambda t: None
        )
    with pytest.raises(BackupError):
        backups.list_remote(conn, "r:b", "web-1; reboot")
    assert conn.sent == []


def test_a_dry_run_that_fails_raises_with_the_servers_words():
    class Failing(Recording):
        def run(self, command, timeout=60):
            return Result(1, "", "backup.mode is 'x'")

    with pytest.raises(BackupError, match="backup.mode"):
        backups.dry_run(Failing())


# ---- against a server --------------------------------------------------------


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


@pytest.fixture
def root(conn):
    path = f"/tmp/hf-backups-{uuid.uuid4().hex}"
    yield path
    conn.run(f"rm -rf {path}")


@needs_bash
def test_a_snapshot_goes_to_the_server_and_comes_back_identical(conn, root, tmp_path):
    uploaded = push_from_pc(conn, SNAPSHOT, root)
    assert uploaded == f"{root}/20260920-231500"

    listing = list_server(conn, root)
    assert [s.name for s in listing.snapshots] == ["20260920-231500"]
    assert listing.snapshots[0].project_dirs == ["/root/myapp"]

    back = pull_to_pc(conn, uploaded, tmp_path / "pc", "web-1")

    assert back == tmp_path / "pc" / "web-1" / "20260920-231500"
    original = sorted(
        p.relative_to(SNAPSHOT).as_posix() for p in SNAPSHOT.rglob("*") if p.is_file()
    )
    copied = sorted(
        p.relative_to(back).as_posix() for p in back.rglob("*") if p.is_file()
    )
    assert copied == original
    for name in original:
        assert (back / name).read_bytes() == (SNAPSHOT / name).read_bytes()


@needs_bash
def test_a_snapshot_is_not_copied_over_one_that_is_already_there(conn, root, tmp_path):
    push_from_pc(conn, SNAPSHOT, root)
    with pytest.raises(BackupError, match="already on the server"):
        push_from_pc(conn, SNAPSHOT, root)

    pull_to_pc(conn, f"{root}/20260920-231500", tmp_path, "web-1")
    with pytest.raises(BackupError, match="already on this computer"):
        pull_to_pc(conn, f"{root}/20260920-231500", tmp_path, "web-1")


@needs_bash
def test_the_server_list_is_newest_first_and_reports_a_broken_manifest(conn, root):
    push_from_pc(conn, SNAPSHOT, root)
    conn.run(
        f"mkdir -p {root}/20250101-000000 && echo '{{nope' > {root}/20250101-000000/manifest.json"
    )
    conn.run(
        f"mkdir -p {root}/20271231-235959 && cp {root}/20260920-231500/manifest.json {root}/20271231-235959/"
    )
    conn.run(f"mkdir -p {root}/not-a-snapshot")

    listing = list_server(conn, root)

    assert len(listing.snapshots) == 2
    assert len(listing.problems) == 1 and "20250101-000000" in listing.problems[0]


@needs_bash
def test_the_key_file_is_written_closed_and_can_be_removed(conn):
    path = f"/tmp/hf-key-{uuid.uuid4().hex}"
    try:
        conn.put_bytes(path, b"AGE-SECRET-KEY-1TEST\n")
        assert conn.run(f"cat {path}").out == "AGE-SECRET-KEY-1TEST\n"
    finally:
        conn.run(f"rm -f {path}")
