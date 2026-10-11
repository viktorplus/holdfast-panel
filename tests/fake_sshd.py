"""A small in-process SSH server for tests.

Commands are really run with bash, with HOME pointed at a temporary directory,
so `authorized_keys` is a real file and a public-key login is accepted only if
the key was genuinely written there.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import paramiko

USER = "root"
PASSWORD = "correct horse"


def find_bash() -> str | None:
    """A bash that honours HOME.

    Not the first `bash` on a Windows PATH: that is System32\bash.exe, which
    starts WSL and ignores the temporary HOME, so the test would write into
    the user's own WSL home.
    """
    if os.name != "nt":
        return shutil.which("bash")
    for candidate in (
        os.environ.get("GIT_BASH", ""),
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files (x86)/Git/bin/bash.exe",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class _Handler(paramiko.ServerInterface):
    def __init__(self, home: Path):
        self.home = home

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        ok = username == USER and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        keys_file = self.home / ".ssh" / "authorized_keys"
        if username != USER or not keys_file.exists():
            return paramiko.AUTH_FAILED
        for line in keys_file.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == key.get_base64():
                return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )

    def check_channel_exec_request(self, channel, command):
        def work():
            done = subprocess.run(
                [find_bash(), "-c", command.decode()],
                capture_output=True,
                check=False,
                env={**os.environ, "HOME": str(self.home)},
            )
            channel.sendall(done.stdout)
            channel.sendall_stderr(done.stderr)
            channel.send_exit_status(done.returncode)
            channel.close()

        threading.Thread(target=work, daemon=True).start()
        return True


class FakeSshd:
    def __init__(self, home: Path):
        self.home = home
        self.host_key = paramiko.ECDSAKey.generate()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        transport = paramiko.Transport(conn)
        transport.add_server_key(self.host_key)
        try:
            transport.start_server(server=_Handler(self.home))
        except (paramiko.SSHException, EOFError, OSError):
            pass

    def close(self):
        self._sock.close()
