import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash

from holdfast_panel.enroll import authorize_command, enroll
from holdfast_panel.keys import ensure_keypair
from holdfast_panel.ssh import (
    HostKeyMismatch,
    SshError,
    connect,
    fetch_host_fingerprint,
)
from holdfast_panel.store import Server, ServerStore, StoreError

needs_bash = pytest.mark.skipif(
    find_bash() is None, reason="needs a bash that honours HOME"
)


class MemorySecrets:
    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


# ---- store -----------------------------------------------------------------


def test_the_list_survives_a_reload_and_holds_no_password(tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    store.add(Server(name="web-1", host="10.0.0.1", port=2222, user="deploy"))

    again = ServerStore(tmp_path / "servers.json").load()

    assert [(s.name, s.host, s.port, s.user) for s in again] == [
        ("web-1", "10.0.0.1", 2222, "deploy")
    ]
    assert "password" not in (tmp_path / "servers.json").read_text().lower()


def test_a_second_server_with_the_same_name_is_refused(tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    store.add(Server(name="web-1", host="a"))
    with pytest.raises(StoreError):
        store.add(Server(name="web-1", host="b"))


def test_update_and_remove_touch_only_their_server(tmp_path):
    store = ServerStore(tmp_path / "servers.json")
    first, second = Server(name="a", host="h1"), Server(name="b", host="h2")
    store.add(first)
    store.add(second)

    first.enrolled = True
    store.update(first)
    store.remove(second.id)

    left = store.load()
    assert [(s.name, s.enrolled) for s in left] == [("a", True)]


def test_a_damaged_file_is_an_error_not_an_empty_list(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        ServerStore(path).load()
    assert path.read_text(encoding="utf-8") == "{not json"


# ---- keys ------------------------------------------------------------------


def test_the_key_is_made_once_and_opens_with_its_passphrase(tmp_path):
    secrets = MemorySecrets()
    first = ensure_keypair(tmp_path, secrets)
    again = ensure_keypair(tmp_path, secrets)

    assert first.public_line == again.public_line
    assert first.public_line.startswith("ssh-ed25519 ")
    assert first.load().get_base64() in first.public_line
    # The file alone does not open: the passphrase is held elsewhere.
    assert first.passphrase.encode() not in first.private_path.read_bytes()


def test_a_key_without_its_passphrase_is_reported_not_replaced(tmp_path):
    ensure_keypair(tmp_path, MemorySecrets())
    with pytest.raises(RuntimeError):
        ensure_keypair(tmp_path, MemorySecrets())


def test_only_a_real_public_key_line_can_enter_a_shell_command():
    with pytest.raises(ValueError):
        authorize_command("ssh-ed25519 AAAA x'; rm -rf / #")


# ---- ssh and enrolment -----------------------------------------------------


@pytest.fixture
def sshd(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    server = FakeSshd(home)
    yield server
    server.close()


def accepted(sshd):
    fingerprint = fetch_host_fingerprint("127.0.0.1", sshd.port)
    return Server(
        name="t", host="127.0.0.1", port=sshd.port, host_fingerprint=fingerprint
    )


def test_the_fingerprint_is_fetched_without_logging_in(sshd):
    assert fetch_host_fingerprint("127.0.0.1", sshd.port).startswith("SHA256:")


def test_an_unreachable_server_is_explained():
    with pytest.raises(SshError, match="cannot reach"):
        fetch_host_fingerprint("127.0.0.1", 1)


def test_nothing_connects_before_the_host_key_is_accepted(sshd):
    server = Server(name="t", host="127.0.0.1", port=sshd.port)
    with pytest.raises(SshError, match="not been accepted"):
        connect(server, password=PASSWORD)


def test_a_changed_host_key_stops_the_connection(sshd):
    server = accepted(sshd)
    server.host_fingerprint = "SHA256:somebodyelse"
    with pytest.raises(HostKeyMismatch):
        connect(server, password=PASSWORD)


@needs_bash
def test_enrolment_installs_the_key_and_the_key_then_logs_in(sshd, tmp_path):
    key = ensure_keypair(tmp_path / "keys", MemorySecrets())
    server = accepted(sshd)

    with pytest.raises(SshError, match="key was refused"):
        connect(server, key=key)  # not there yet

    enroll(server, PASSWORD, key)

    authorized = (sshd.home / ".ssh" / "authorized_keys").read_text()
    assert authorized.count(key.public_line) == 1
    with connect(server, key=key) as session:
        assert session.run("echo hello").out == "hello\n"


@needs_bash
def test_enrolling_twice_does_not_duplicate_the_key(sshd, tmp_path):
    key = ensure_keypair(tmp_path / "keys", MemorySecrets())
    server = accepted(sshd)

    enroll(server, PASSWORD, key)
    enroll(server, PASSWORD, key)

    authorized = (sshd.home / ".ssh" / "authorized_keys").read_text()
    assert authorized.count(key.public_line) == 1


def test_a_wrong_password_is_reported_and_installs_nothing(sshd, tmp_path):
    key = ensure_keypair(tmp_path / "keys", MemorySecrets())
    with pytest.raises(SshError, match="password was refused"):
        enroll(accepted(sshd), "wrong", key)
    assert not (sshd.home / ".ssh").exists()


# ---- storages --------------------------------------------------------------


def test_a_server_file_written_before_backup_root_existed_still_loads(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(
        '{"servers": [{"name": "old", "host": "h", "id": "1"}]}', encoding="utf-8"
    )
    (server,) = ServerStore(path).load()
    assert server.backup_root == "/opt/backups"


def test_storages_are_kept_and_checked(tmp_path):
    from holdfast_panel.store import Storage, StorageStore

    store = StorageStore(tmp_path / "storages.json")
    store.add(Storage(name="Мой ноутбук", kind="pc", location="D:/backups"))
    store.add(Storage(name="Хранилище 1", kind="remote", location="shared:backups"))

    again = StorageStore(tmp_path / "storages.json").load()
    assert [(s.name, s.kind) for s in again] == [
        ("Мой ноутбук", "pc"),
        ("Хранилище 1", "remote"),
    ]
    with pytest.raises(StoreError, match="already exists"):
        store.add(Storage(name="Мой ноутбук", kind="pc", location="E:/x"))
    with pytest.raises(StoreError, match="remote:folder"):
        store.add(Storage(name="bad", kind="remote", location="no-colon"))
    with pytest.raises(StoreError, match="kind"):
        store.add(Storage(name="bad2", kind="cloud", location="x"))
    with pytest.raises(StoreError):
        store.add(Storage(name="  ", kind="pc", location="x"))
    store.remove(again[0].id)
    assert [s.name for s in store.load()] == ["Хранилище 1"]
