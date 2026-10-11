"""The panel's own SSH key.

One ed25519 key for the whole panel, created on first use. Its private half is
encrypted with a random passphrase that lives in the Windows Credential
Manager, so the key file alone opens nothing.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SERVICE = "holdfast-panel"
ACCOUNT = "ssh-key-passphrase"
COMMENT = "holdfast-panel"


class Secrets(Protocol):
    def get(self) -> str | None: ...

    def set(self, value: str) -> None: ...


class CredentialManager:
    """The passphrase in Windows Credential Manager, through `keyring`."""

    def get(self) -> str | None:
        import keyring

        return keyring.get_password(SERVICE, ACCOUNT)

    def set(self, value: str) -> None:
        import keyring

        keyring.set_password(SERVICE, ACCOUNT, value)


@dataclass(frozen=True)
class KeyPair:
    private_path: Path
    public_line: str
    """`ssh-ed25519 AAAA... holdfast-panel`: one line for authorized_keys."""
    passphrase: str

    def load(self) -> paramiko.PKey:
        return paramiko.Ed25519Key.from_private_key_file(
            str(self.private_path), password=self.passphrase
        )


def ensure_keypair(directory: Path, secrets_store: Secrets) -> KeyPair:
    private_path = directory / "id_ed25519"
    public_path = directory / "id_ed25519.pub"
    phrase = secrets_store.get()

    if private_path.exists() and public_path.exists():
        if not phrase:
            raise RuntimeError(
                f"{private_path} exists but its passphrase is not in the "
                "Credential Manager; the key cannot be opened"
            )
        return KeyPair(private_path, public_path.read_text("ascii").strip(), phrase)

    phrase = phrase or secrets.token_urlsafe(32)
    # Stored before the key is written: a key without its passphrase is lost.
    secrets_store.set(phrase)
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(phrase.encode()),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    )
    line = f"{public.decode('ascii')} {COMMENT}"
    directory.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private)
    public_path.write_text(line + "\n", encoding="ascii")
    return KeyPair(private_path, line, phrase)
