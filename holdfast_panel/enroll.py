"""Put the panel's public key on a server, once, using its password."""

from __future__ import annotations

import re

from .keys import KeyPair
from .ssh import SshError, connect
from .store import Server

# The line goes into a shell command, so only the characters of a real
# ed25519 public key line may reach it.
_PUBLIC_LINE = re.compile(r"^ssh-ed25519 [A-Za-z0-9+/=]+ [A-Za-z0-9._-]+$")


def authorize_command(public_line: str) -> str:
    """A shell line that adds the key to authorized_keys unless it is there."""
    if not _PUBLIC_LINE.match(public_line):
        raise ValueError("not a public key line")
    return (
        "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
        f"(grep -qxF '{public_line}' ~/.ssh/authorized_keys || "
        f"echo '{public_line}' >> ~/.ssh/authorized_keys) && "
        "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
    )


def enroll(server: Server, password: str, key: KeyPair) -> None:
    """Install the key with the password, then prove it works without one.

    The password is an argument of this function and nothing else: it is not
    stored, logged or returned. The caller marks the server enrolled only
    after this returns.
    """
    with connect(server, password=password) as session:
        result = session.run(authorize_command(key.public_line))
        if not result.ok:
            raise SshError(
                f"could not write ~/.ssh/authorized_keys: {result.err.strip()}"
            )
    with connect(server, key=key) as session:
        if session.run("echo ok").out.strip() != "ok":
            raise SshError("the key was installed, but a login with it failed")
