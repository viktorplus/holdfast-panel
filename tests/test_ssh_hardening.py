"""Findings of the security review of the SSH layer, each with a test that fails
on the code before the fix."""

import io
import os
import tarfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash
from test_core import MemorySecrets

from holdfast_panel.enroll import authorize_command
from holdfast_panel.keys import KeyPair, KeyStoreError, ensure_keypair
from holdfast_panel.ssh import (
    Connection,
    SshError,
    connect,
    fetch_host_fingerprint,
)
from holdfast_panel.store import Server, ServerStore, StoreError

needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs a bash with HOME")


# ---- a channel that says whatever the test wants ----------------------------------------


class FakeChannel:
    def __init__(self, data: bytes, code: int = 0, stderr: bytes = b""):
        self._data, self.code, self.stderr = data, code, stderr
        self.status_event = threading.Event()
        self.status_event.set()

    def shutdown_write(self):
        pass

    def makefile(self, mode):
        return io.BytesIO(self._data)

    def recv_exit_status(self):
        return self.code

    def recv_stderr(self, n):
        return self.stderr

    def close(self):
        pass


def tar_of(members: dict[str, bytes], cut: int | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    data = buffer.getvalue()
    return data if cut is None else data[:cut]


def download(monkeypatch, tmp_path, channel, remote="/opt/backups/20260920-231500"):
    conn = Connection(None)
    monkeypatch.setattr(conn, "_session", lambda command, combine_stderr=False: channel)
    return conn.download_dir(remote, tmp_path / "pc" / "web-1")


NAME = "20260920-231500"


def test_a_good_stream_lands_under_its_name_and_leaves_no_scratch(
    monkeypatch, tmp_path
):
    data = tar_of({f"{NAME}/manifest.json": b"{}", f"{NAME}/a.bin": b"abc"})
    final = download(monkeypatch, tmp_path, FakeChannel(data))

    assert (final / "manifest.json").read_text() == "{}"
    assert [p.name for p in final.parent.iterdir()] == [NAME]  # no .part left


def test_a_server_cannot_overwrite_a_sibling_snapshot(monkeypatch, tmp_path):
    parent = tmp_path / "pc" / "web-1"
    good = parent / "20240101-000000"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text("GOOD")
    data = tar_of(
        {f"{NAME}/manifest.json": b"{}", "20240101-000000/manifest.json": b"PWNED"}
    )

    with pytest.raises(SshError, match="outside the folder"):
        download(monkeypatch, tmp_path, FakeChannel(data))

    assert (good / "manifest.json").read_text() == "GOOD"
    assert not (parent / NAME).exists()
    assert [p.name for p in parent.iterdir()] == ["20240101-000000"]


@pytest.mark.parametrize(
    "member", [f"../{NAME}x/evil", f"/abs/{NAME}/evil", f"{NAME}/../../escape"]
)
def test_paths_that_climb_out_are_refused(monkeypatch, tmp_path, member):
    data = tar_of({member: b"x"})
    with pytest.raises(SshError):
        download(monkeypatch, tmp_path, FakeChannel(data))
    assert not (tmp_path / "pc" / "web-1" / NAME).exists()
    assert not (tmp_path / "escape").exists()


def test_an_interrupted_download_leaves_nothing_and_can_be_retried(
    monkeypatch, tmp_path
):
    data = tar_of({f"{NAME}/manifest.json": b"{}", f"{NAME}/big.bin": b"x" * 5000})
    parent = tmp_path / "pc" / "web-1"

    with pytest.raises(SshError, match="damaged|failed"):
        download(monkeypatch, tmp_path, FakeChannel(data[:1700]))
    assert not (parent / NAME).exists()

    final = download(monkeypatch, tmp_path, FakeChannel(data))  # the retry works
    assert (final / "big.bin").stat().st_size == 5000
    assert [p.name for p in parent.iterdir()] == [NAME]


def test_a_tar_that_failed_on_the_server_is_not_kept_even_if_it_extracted(
    monkeypatch, tmp_path
):
    data = tar_of({f"{NAME}/manifest.json": b"{}"})
    channel = FakeChannel(data, code=2, stderr=b"tar: disk error")

    with pytest.raises(SshError, match="disk error"):
        download(monkeypatch, tmp_path, channel)
    assert not (tmp_path / "pc" / "web-1" / NAME).exists()


def test_a_destination_that_exists_is_never_replaced(monkeypatch, tmp_path):
    existing = tmp_path / "pc" / "web-1" / NAME
    existing.mkdir(parents=True)
    (existing / "keep").write_text("mine")
    with pytest.raises(SshError, match="already exists"):
        download(monkeypatch, tmp_path, FakeChannel(tar_of({f"{NAME}/x": b"1"})))
    assert (existing / "keep").read_text() == "mine"


# ---- against a real (in-process) server ------------------------------------------------------


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
def test_a_command_that_floods_stderr_does_not_stall_the_reader(conn):
    # Reading stdout to its end before stderr deadlocks once stderr fills the window.
    result = conn.run(
        "echo done; head -c 3000000 /dev/zero | tr '\\0' x >&2", timeout=40
    )

    assert result.out == "done\n"
    assert len(result.err) == 3_000_000


@needs_bash
def test_a_command_that_never_answers_times_out_with_a_clear_error(conn):
    started = time.monotonic()
    with pytest.raises(SshError, match="no answer in 1s"):
        conn.run("sleep 8", timeout=1)
    assert time.monotonic() - started < 6


@needs_bash
def test_a_stream_that_never_stops_printing_still_hits_its_deadline(conn):
    started = time.monotonic()
    with pytest.raises(SshError, match="longer than 1s"):
        conn.run_stream(
            "while true; do echo tick; sleep 0.05; done", lambda t: None, timeout=1
        )
    assert time.monotonic() - started < 8


@needs_bash
def test_an_upload_is_dated_now_so_the_servers_retention_counts_from_today(
    conn, tmp_path
):
    local = tmp_path / "20000101-000000"
    local.mkdir()
    old = local / "manifest.json"
    old.write_text("{}")
    os.utime(old, (946684800, 946684800))  # the year 2000
    folder = f"/tmp/hf-up-{uuid.uuid4().hex}"
    try:
        conn.upload_dir(local, folder)
        mtime = int(conn.run(f"stat -c %Y {folder}/20000101-000000/manifest.json").out)
        assert abs(mtime - time.time()) < 120
    finally:
        conn.run(f"rm -rf {folder}")


@needs_bash
def test_an_upload_refuses_anything_but_plain_files_and_folders(conn, tmp_path):
    local = tmp_path / "20260920-231500"
    local.mkdir()
    (local / "ok").write_text("x")
    try:
        os.symlink(local / "ok", local / "link")
    except (OSError, NotImplementedError):
        pytest.skip("this system does not let the test create a symlink")
    with pytest.raises(SshError, match="plain files"):
        conn.upload_dir(local, f"/tmp/hf-up-{uuid.uuid4().hex}")


# ---- the key and what surrounds it ---------------------------------------------------------------


def test_the_passphrase_is_not_in_the_repr_of_a_key_pair():
    pair = KeyPair(Path("k"), "ssh-ed25519 AAAA x", "TOPSECRET-PASSPHRASE")
    assert "TOPSECRET" not in repr(pair) and "TOPSECRET" not in str(pair)


def test_one_key_file_without_the_other_is_an_error_never_a_new_key(tmp_path):
    ensure_keypair(tmp_path, MemorySecrets())
    (tmp_path / "id_ed25519.pub").unlink()
    private_before = (tmp_path / "id_ed25519").read_bytes()

    with pytest.raises(KeyStoreError, match="only one of"):
        ensure_keypair(tmp_path, MemorySecrets())

    assert (tmp_path / "id_ed25519").read_bytes() == private_before


def test_two_workers_asking_at_once_make_one_key(tmp_path):
    secrets = MemorySecrets()
    results = []

    def ask():
        results.append(ensure_keypair(tmp_path, secrets).public_line)

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6 and len(set(results)) == 1


def test_a_failing_credential_store_is_a_readable_error(tmp_path):
    class Broken(MemorySecrets):
        def get(self):
            raise KeyStoreError("cannot read the key passphrase: locked")

    with pytest.raises(KeyStoreError, match="locked"):
        ensure_keypair(tmp_path, Broken())


def test_a_key_line_with_a_trailing_newline_is_not_a_key_line():
    with pytest.raises(ValueError):
        authorize_command("ssh-ed25519 AAAA c\n")


def test_renaming_onto_a_taken_name_or_updating_a_removed_record_is_refused(tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    a, b = Server(name="a", host="1"), Server(name="b", host="2")
    store.add(a)
    store.add(b)

    b.name = "a"
    with pytest.raises(StoreError, match="already exists"):
        store.update(b)

    store.remove(a.id)
    a.host = "changed"
    with pytest.raises(StoreError, match="removed meanwhile"):
        store.update(a)
    assert [s.name for s in store.load()] == ["b"]
