"""SSH, in the forms the panel needs.

A host key is never accepted silently: the fingerprint is fetched first, shown
to the user, stored on the server record, and every later connection must
match it.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
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


class Connection:
    def __init__(self, client: paramiko.SSHClient):
        self._client = client

    def run(self, command: str, timeout: float = 60) -> Result:
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
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
