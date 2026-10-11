"""SSH, in the forms the panel needs.

A host key is never accepted silently: the fingerprint is fetched first, shown
to the user, stored on the server record, and every later connection must
match it.

Nothing here may wait for ever on a server that stalls: every read has a
timeout, a finished command must report its exit status within a minute, and a
silent link is detected by keepalive.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import secrets
import shlex
import shutil
import socket
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import paramiko

from .keys import KeyPair
from .store import Server

CONNECT_TIMEOUT = 15
IO_TIMEOUT = 120
"""One read or write on a channel may stall this long before it is an error."""
STATUS_WAIT = 60
"""After the output ended, the server has this long to report the exit status."""
KEEPALIVE = 30


class SshError(Exception):
    """Something the user can read and act on."""


class HostKeyMismatch(SshError):
    pass


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def fingerprint_of(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def fetch_host_fingerprint(host: str, port: int) -> str:
    """Ask the server for its host key and close. Nothing is sent to it."""
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        raise SshError(f"cannot reach {host}:{port}: {exc}") from exc
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=CONNECT_TIMEOUT)
        return fingerprint_of(transport.get_remote_server_key())
    except (paramiko.SSHException, EOFError) as exc:
        raise SshError(f"{host}:{port} did not answer as an SSH server: {exc}") from exc
    finally:
        transport.close()


class _PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = fingerprint_of(key)
        if actual != self.expected:
            raise HostKeyMismatch(
                f"the host key of {hostname} is {actual}, but {self.expected} was "
                "accepted before. The server was reinstalled, or the connection "
                "is being intercepted; nothing was sent."
            )


class _Counting:
    """A file object that reports how many bytes went through it."""

    def __init__(self, inner, on_bytes: Callable[[int], None]):
        self._inner = inner
        self._on_bytes = on_bytes

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        self._on_bytes(len(data))
        return data

    def write(self, data: bytes) -> int:
        written = self._inner.write(data)
        self._on_bytes(len(data))
        return written


class _ChannelWriter:
    def __init__(self, channel: paramiko.Channel):
        self._channel = channel

    def write(self, data: bytes) -> int:
        self._channel.sendall(data)
        return len(data)


def _status(channel: paramiko.Channel, what: str) -> int:
    """The exit status, waiting a bounded time for the server to send it."""
    if not channel.status_event.wait(STATUS_WAIT):
        channel.close()
        raise SshError(f"{what}: the server did not say how the command ended")
    return channel.recv_exit_status()


def _stderr_of(channel: paramiko.Channel) -> str:
    try:
        return channel.recv_stderr(4096).decode("utf-8", "replace").strip()
    except (OSError, EOFError):
        return ""


class Connection:
    def __init__(self, client: paramiko.SSHClient):
        self._client = client

    def _session(self, command: str, combine_stderr: bool = False) -> paramiko.Channel:
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise SshError("the connection to the server is closed")
        channel = transport.open_session(timeout=IO_TIMEOUT)
        channel.settimeout(IO_TIMEOUT)
        if combine_stderr:
            channel.set_combine_stderr(True)
        channel.exec_command(command)
        return channel

    def run(self, command: str, timeout: float = 60) -> Result:
        """Run a short command and collect its output.

        Both streams are drained together: reading one to its end first would
        stall for good on a command that fills the other.
        """
        channel = self._session(command)
        channel.shutdown_write()  # nothing is sent; `cat` sees the end of input
        out, err = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            while True:
                got = False
                if channel.recv_ready():
                    out += channel.recv(65536)
                    got = True
                if channel.recv_stderr_ready():
                    err += channel.recv_stderr(65536)
                    got = True
                if (
                    (channel.eof_received or channel.closed)
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if not got:
                    if time.monotonic() > deadline:
                        raise SshError(
                            f"no answer in {timeout:.0f}s to: {command[:80]}"
                        )
                    time.sleep(0.01)
            code = _status(channel, command[:80])
        finally:
            channel.close()
        return Result(
            code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        )

    def run_stream(
        self, command: str, on_text: Callable[[str], None], timeout: float = 1800
    ) -> int:
        """Run a long command, handing its output (stderr merged) over as it comes.

        `timeout` bounds the whole command, not one read: a step that prints
        nothing for an hour is a hung step, and so is one that never stops
        printing.
        """
        channel = self._session(command, combine_stderr=True)
        channel.settimeout(1.0)
        channel.shutdown_write()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    raise SshError(f"the command ran longer than {timeout:.0f}s")
                try:
                    chunk = channel.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                on_text(decoder.decode(chunk))
            tail = decoder.decode(b"", final=True)
            if tail:
                on_text(tail)
            return _status(channel, command[:80])
        finally:
            channel.close()

    def put_bytes(self, path: str, data: bytes, mode: str = "600") -> None:
        """Write a file on the server. Created closed (umask 077), then chmod."""
        quoted = shlex.quote(path)
        channel = self._session(f"umask 077; cat > {quoted} && chmod {mode} {quoted}")
        try:
            channel.sendall(data)
            channel.shutdown_write()
            if _status(channel, f"writing {path}") != 0:
                raise SshError(f"could not write {path}: {_stderr_of(channel)}")
        except (OSError, EOFError) as exc:
            raise SshError(f"could not write {path}: {exc}") from exc
        finally:
            channel.close()

    def download_dir(
        self,
        remote_dir: str,
        local_parent: Path,
        on_bytes: Callable[[int], None] = lambda n: None,
    ) -> Path:
        """Copy a directory from the server as one tar stream.

        The folder `name` lands as `local_parent/name`, or not at all: it is
        extracted beside it under a temporary name and moved into place only
        when the stream ended cleanly, so an interrupted copy never looks like a
        snapshot. Every member must live under `name`, and extraction uses the
        "data" filter, so nothing the server sends can write anywhere else.
        """
        parent, _, name = remote_dir.rstrip("/").rpartition("/")
        if not name:
            raise SshError(f"{remote_dir!r} is not a folder to copy")
        final = local_parent / name
        if final.exists():
            raise SshError(f"{final} already exists")
        local_parent.mkdir(parents=True, exist_ok=True)
        staging = local_parent / f".{name}.part-{secrets.token_hex(4)}"
        staging.mkdir()

        def inside(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
            if member.name != name and not member.name.startswith(f"{name}/"):
                raise SshError(
                    f"{remote_dir}: the server sent {member.name!r}, which is "
                    "outside the folder that was asked for"
                )
            return tarfile.data_filter(member, dest)

        channel = self._session(
            f"tar -C {shlex.quote(parent or '/')} -cf - -- {shlex.quote(name)}"
        )
        channel.shutdown_write()
        reader = _Counting(channel.makefile("rb"), on_bytes)
        try:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                tar.extractall(staging, filter=inside)
            code = _status(channel, f"copying {remote_dir}")
            if code != 0:
                raise SshError(
                    f"{remote_dir}: tar failed on the server: {_stderr_of(channel)}"
                )
            if not (staging / name).is_dir():
                raise SshError(f"{remote_dir}: the server sent nothing")
            (staging / name).replace(final)
        except tarfile.TarError as exc:
            raise SshError(f"{remote_dir}: the copy is damaged: {exc}") from exc
        except (OSError, EOFError) as exc:
            raise SshError(f"{remote_dir}: the copy failed: {exc}") from exc
        finally:
            channel.close()
            shutil.rmtree(staging, ignore_errors=True)
        return final

    def upload_dir(
        self,
        local_dir: Path,
        remote_parent: str,
        on_bytes: Callable[[int], None] = lambda n: None,
    ) -> str:
        """Copy a local directory to `remote_parent/<its name>` as one tar stream.

        Only plain files and folders are sent; the server gets them owned by
        root, closed (0600/0700), and dated now, so the server's own retention
        counts from the day the snapshot was put back, not the day it was made.
        """
        name = local_dir.name
        if not name:
            raise SshError(f"{local_dir} is not a folder to send")
        now = time.time()

        def private(info: tarfile.TarInfo) -> tarfile.TarInfo:
            if not (info.isfile() or info.isdir()):
                raise SshError(f"{info.name}: only plain files and folders are sent")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o700 if info.isdir() else 0o600
            info.mtime = now
            return info

        channel = self._session(
            f"mkdir -p {shlex.quote(remote_parent)} && "
            f"tar -C {shlex.quote(remote_parent)} --no-same-owner -xf -"
        )
        writer = _Counting(_ChannelWriter(channel), on_bytes)
        try:
            with tarfile.open(fileobj=writer, mode="w|") as tar:
                tar.add(local_dir, arcname=name, filter=private)
            channel.shutdown_write()
            if _status(channel, f"sending {name}") != 0:
                raise SshError(
                    f"the upload failed on the server: {_stderr_of(channel)}"
                )
        except (OSError, EOFError) as exc:
            # the server stopped listening: its own words say why
            raise SshError(f"the upload stopped: {_stderr_of(channel) or exc}") from exc
        finally:
            channel.close()
        return f"{remote_parent.rstrip('/')}/{name}"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(
    server: Server, *, key: KeyPair | None = None, password: str | None = None
) -> Connection:
    """Log in with the panel's key, or with a password for the first time."""
    if not server.host_fingerprint:
        raise SshError(f"the host key of {server.name!r} has not been accepted yet")
    if (key is None) == (password is None):
        raise ValueError("give exactly one of key and password")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_PinnedHostKey(server.host_fingerprint))
    try:
        client.connect(
            server.host,
            port=server.port,
            username=server.user,
            pkey=key.load() if key else None,
            password=password,
            timeout=CONNECT_TIMEOUT,
            banner_timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        client.get_transport().set_keepalive(KEEPALIVE)
    except paramiko.AuthenticationException as exc:
        what = "key" if key else "password"
        raise SshError(f"{server.user}@{server.host}: the {what} was refused") from exc
    except HostKeyMismatch:
        client.close()
        raise
    except (paramiko.SSHException, OSError, EOFError) as exc:
        client.close()
        raise SshError(f"cannot connect to {server.host}:{server.port}: {exc}") from exc
    return Connection(client)
