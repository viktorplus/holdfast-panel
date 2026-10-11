"""SSH, in the forms the panel needs.

A host key is never accepted silently: the fingerprint is fetched first, shown
to the user, stored on the server record, and every later connection must
match it.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import shlex
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


class Connection:
    def __init__(self, client: paramiko.SSHClient):
        self._client = client

    def run(self, command: str, timeout: float = 60) -> Result:
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        # Nothing is sent to a command; closing stdin lets `cat` and friends
        # see the end of input instead of waiting for it forever.
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        return Result(stdout.channel.recv_exit_status(), out, err)

    def run_stream(
        self, command: str, on_text: Callable[[str], None], timeout: float = 1800
    ) -> int:
        """Run a long command, handing its output (stderr merged) over as it comes.

        `timeout` bounds the whole command, not one read: a step that prints
        nothing for an hour is a hung step.
        """
        channel = self._client.get_transport().open_session()
        channel.set_combine_stderr(True)
        channel.settimeout(1.0)
        channel.exec_command(command)
        channel.shutdown_write()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        deadline = time.monotonic() + timeout
        while True:
            try:
                chunk = channel.recv(4096)
            except TimeoutError:
                if time.monotonic() > deadline:
                    channel.close()
                    raise SshError(
                        f"the command ran longer than {timeout:.0f}s"
                    ) from None
                continue
            if not chunk:
                break
            on_text(decoder.decode(chunk))
        tail = decoder.decode(b"", final=True)
        if tail:
            on_text(tail)
        return channel.recv_exit_status()

    def _session(self, command: str) -> paramiko.Channel:
        channel = self._client.get_transport().open_session()
        channel.exec_command(command)
        return channel

    def put_bytes(self, path: str, data: bytes, mode: str = "600") -> None:
        """Write a file on the server. Created closed (umask 077), then chmod."""
        quoted = shlex.quote(path)
        channel = self._session(f"umask 077; cat > {quoted} && chmod {mode} {quoted}")
        channel.sendall(data)
        channel.shutdown_write()
        code = channel.recv_exit_status()
        if code != 0:
            err = channel.recv_stderr(4096).decode("utf-8", "replace").strip()
            raise SshError(f"could not write {path}: {err}")

    def download_dir(
        self,
        remote_dir: str,
        local_parent: Path,
        on_bytes: Callable[[int], None] = lambda n: None,
    ) -> Path:
        """Copy a directory from the server as one tar stream.

        The folder `name` lands as `local_parent/name`. Extraction uses the
        "data" filter, so nothing the server sends can escape that folder.
        """
        parent, _, name = remote_dir.rstrip("/").rpartition("/")
        channel = self._session(
            f"tar -C {shlex.quote(parent or '/')} -cf - {shlex.quote(name)}"
        )
        channel.shutdown_write()
        local_parent.mkdir(parents=True, exist_ok=True)
        reader = _Counting(channel.makefile("rb"), on_bytes)
        try:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                tar.extractall(local_parent, filter="data")
        except tarfile.TarError as exc:
            raise SshError(f"{remote_dir}: the copy is damaged: {exc}") from exc
        code = channel.recv_exit_status()
        if code != 0:
            err = channel.recv_stderr(4096).decode("utf-8", "replace").strip()
            raise SshError(f"{remote_dir}: tar failed on the server: {err}")
        return local_parent / name

    def upload_dir(
        self,
        local_dir: Path,
        remote_parent: str,
        on_bytes: Callable[[int], None] = lambda n: None,
    ) -> str:
        """Copy a local directory to `remote_parent/<its name>` as one tar stream."""
        channel = self._session(
            f"mkdir -p {shlex.quote(remote_parent)} && "
            f"tar -C {shlex.quote(remote_parent)} -xf -"
        )

        def private(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o700 if info.isdir() else 0o600
            return info

        writer = _Counting(_ChannelWriter(channel), on_bytes)
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            tar.add(local_dir, arcname=local_dir.name, filter=private)
        channel.shutdown_write()
        code = channel.recv_exit_status()
        if code != 0:
            err = channel.recv_stderr(4096).decode("utf-8", "replace").strip()
            raise SshError(f"the upload failed on the server: {err}")
        return f"{remote_parent.rstrip('/')}/{local_dir.name}"

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
