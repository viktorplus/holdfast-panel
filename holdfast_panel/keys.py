"""The panel's own SSH key.

One ed25519 key for the whole panel, created on first use. Its private half is
encrypted with a random passphrase that lives in the Windows Credential
Manager, so the key file alone opens nothing.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SERVICE = "holdfast-panel"
ACCOUNT = "ssh-key-passphrase"
COMMENT = "holdfast-panel"


class KeyStoreError(Exception):
    """The panel's key or its passphrase cannot be used; the text says why."""


_LOCK = threading.Lock()


class Secrets(Protocol):
    def get(self) -> str | None: ...

    def set(self, value: str) -> None: ...


class CredentialManager:
    """The passphrase in Windows Credential Manager, through `keyring`."""

    def _keyring(self):
        import keyring

        backend = type(keyring.get_keyring()).__name__
        if "Fail" in backend or "Plaintext" in backend or "Null" in backend:
            raise KeyStoreError(
                f"no secure place for the key passphrase ({backend}); the Windows "
                "Credential Manager is needed"
            )
        return keyring

    def get(self) -> str | None:
        keyring = self._keyring()
        try:
            return keyring.get_password(SERVICE, ACCOUNT)
        except keyring.errors.KeyringError as exc:
            raise KeyStoreError(f"cannot read the key passphrase: {exc}") from exc

    def set(self, value: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(SERVICE, ACCOUNT, value)
        except keyring.errors.KeyringError as exc:
            raise KeyStoreError(f"cannot store the key passphrase: {exc}") from exc


@dataclass(frozen=True)
class KeyPair:
    private_path: Path
    public_line: str
    """`ssh-ed25519 AAAA... holdfast-panel`: one line for authorized_keys."""
    passphrase: str = field(repr=False)

    def load(self) -> paramiko.PKey:
        return paramiko.Ed25519Key.from_private_key_file(
            str(self.private_path), password=self.passphrase
        )


def ensure_keypair(directory: Path, secrets_store: Secrets) -> KeyPair:
    """The panel's key: made once, then only read.

    One of the two files without the other is an error, never a reason to make
    a new key: every server enrolled with the old one would stop answering.
    """
    private_path = directory / "id_ed25519"
    public_path = directory / "id_ed25519.pub"
    with _LOCK:  # several workers may ask at once, the first time
        have = (private_path.exists(), public_path.exists())
        if have == (True, True):
            phrase = secrets_store.get()
            if not phrase:
                raise KeyStoreError(
                    f"{private_path} exists but its passphrase is not in the "
                    "Credential Manager; the key cannot be opened"
                )
            return KeyPair(private_path, public_path.read_text("ascii").strip(), phrase)
        if have != (False, False):
            raise KeyStoreError(
                f"only one of {private_path.name} and {public_path.name} is in "
                f"{directory}; restore the missing file or move both away to start "
                "with a new key (servers will need to be enrolled again)"
            )

        phrase = secrets_store.get() or secrets.token_urlsafe(32)
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
        with private_path.open("xb") as handle:  # never over an existing key
            handle.write(private)
        with public_path.open("x", encoding="ascii") as handle:
            handle.write(line + "\n")
        return KeyPair(private_path, line, phrase)
