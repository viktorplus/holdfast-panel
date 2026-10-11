"""Snapshots in the three places they can live, and moving them between them.

* on the server: `backup.root` (default /opt/backups), one folder per run;
* on this computer: `<folder>/<host_label>/<snapshot>/`;
* on a remote storage: an rclone destination, `<spec>/<host_label>/<snapshot>/`,
  reached through a server that has that rclone remote configured.

Snapshots are read from their own `manifest.json`, which is plain JSON, so the
panel does not depend on the holdfast version installed on the server.

A snapshot is only ever put in place whole: it is built under a hidden name and
renamed when it is complete and checked. A half-copied folder that looks like a
snapshot would be listed, offered for restore, and counted by the server's
retention as the newest copy.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ssh import Connection
from .store import SPEC_PATTERN

DEFAULT_ROOT = "/opt/backups"
_SNAPSHOT_NAME = re.compile(r"\d{8}-\d{6}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SPEC = re.compile(SPEC_PATTERN)
MANIFEST_FORMAT = 1
RCLONE_PROGRESS = ("-v", "--stats", "15s", "--stats-one-line")
"""Plain lines, not `--progress`: that redraws with control codes even without a
terminal and would fill the log with them."""


@dataclass(frozen=True)
class Component:
    """One artifact of a snapshot, with the component it belongs to."""

    name: str
    kind: str
    """What restores it: path, docker_volume, mysql_database, ..."""
    size: int
    target: str = ""
    """For a project component: the folder it was taken from (where compose
    runs). Empty for everything else. Not the recipe's `target`, which holdfast
    always writes as "/" for a folder."""


@dataclass(frozen=True)
class SnapshotInfo:
    name: str
    location: str
    """A path on the server, a path on this computer, or an rclone path."""
    host_label: str = ""
    created_at: str = ""
    encrypted: bool = False
    components: tuple[Component, ...] = ()
    skipped: int = 0
    tool_version: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.components)

    @property
    def project_dirs(self) -> list[str]:
        """Folders of compose projects, in order, each once."""
        found: list[str] = []
        for c in self.components:
            if c.name.startswith("project-") and c.target and c.target not in found:
                found.append(c.target)
        return found


class BackupError(Exception):
    pass


def valid_snapshot_name(name: str) -> str:
    if not _SNAPSHOT_NAME.fullmatch(name):
        raise BackupError(f"{name!r} is not a snapshot name (YYYYMMDD-HHMMSS)")
    return name


def valid_label(label: str) -> str:
    if not _LABEL.fullmatch(label):
        raise BackupError(f"{label!r} is not a usable machine name")
    return label


def valid_spec(spec: str) -> str:
    if not _SPEC.fullmatch(spec):
        raise BackupError(f"{spec!r} is not an rclone path like remote:folder")
    return spec.rstrip("/")


def parse_manifest(text: str, location: str) -> SnapshotInfo:
    try:
        manifest = json.loads(text)
        if manifest.get("format") != MANIFEST_FORMAT:
            raise BackupError(
                f"{location}: snapshot format {manifest.get('format')!r}, "
                f"this panel reads {MANIFEST_FORMAT}"
            )
        # The folder a project came from is in `components`; the artifact's own
        # recipe only says "restore under /".
        folders = {
            str(c.get("name")): str(c.get("path", ""))
            for c in manifest.get("components") or []
            if c.get("type") == "path"
        }
        components = tuple(
            Component(
                name=str(a.get("component", "")),
                kind=str((a.get("restore") or {}).get("type", "")),
                size=int(a.get("size", 0)),
                target=folders.get(str(a.get("component", "")), ""),
            )
            for a in manifest["artifacts"]
        )
        return SnapshotInfo(
            name=str(manifest.get("snapshot") or Path(location).name),
            location=location,
            host_label=str(manifest.get("host_label", "")),
            created_at=str(manifest.get("created_at", "")),
            encrypted=bool((manifest.get("encryption") or {}).get("enabled")),
            components=components,
            skipped=len(manifest.get("skipped") or []),
            tool_version=str(manifest.get("tool_version", "")),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BackupError(f"{location}: the manifest cannot be read: {exc}") from exc


def _split_sections(text: str) -> list[tuple[str, str]]:
    """`##name` lines separate the manifests printed by one command."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith("##"):
            sections.append((line[2:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [(name, "\n".join(lines)) for name, lines in sections]


@dataclass
class Listing:
    snapshots: list[SnapshotInfo] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _read_sections(text: str, base: str) -> Listing:
    listing = Listing()
    for name, body in _split_sections(text):
        if not _SNAPSHOT_NAME.fullmatch(name):
            continue
        try:
            listing.snapshots.append(parse_manifest(body, f"{base}/{name}"))
        except BackupError as exc:
            listing.problems.append(str(exc))
    listing.snapshots.sort(key=lambda s: s.name, reverse=True)
    return listing


# ---- where snapshots are -----------------------------------------------------


def list_server(conn: Connection, root: str = DEFAULT_ROOT) -> Listing:
    """Snapshots in `root`. A missing folder is an error, not an empty list."""
    base = root.rstrip("/")
    quoted = shlex.quote(base)
    if not conn.run(f"test -d {quoted}").ok:
        raise BackupError(f"на сервере нет папки со снимками: {base}")
    result = conn.run(
        f'for d in {quoted}/*/; do n=$(basename "$d"); '
        'if [ -f "$d/manifest.json" ]; then echo "##$n"; cat "$d/manifest.json"; echo; fi; '
        "done",
        timeout=120,
    )
    return _read_sections(result.out, base)


def list_pc(folder: Path, host_label: str | None = None) -> Listing:
    listing = Listing()
    if not folder.is_dir():
        return listing
    labels = [folder / host_label] if host_label else sorted(folder.iterdir())
    for label_dir in labels:
        if not label_dir.is_dir():
            continue
        for snap in sorted(label_dir.iterdir(), reverse=True):
            manifest = snap / "manifest.json"
            if snap.name.startswith(".") or not (snap.is_dir() and manifest.is_file()):
                continue
            try:
                listing.snapshots.append(
                    parse_manifest(manifest.read_text("utf-8"), str(snap))
                )
            except (OSError, BackupError) as exc:
                listing.problems.append(str(exc))
    return listing


def list_remote(conn: Connection, spec: str, host_label: str) -> Listing:
    """Snapshots of one machine on an rclone remote, asked through `conn`.

    "rclone is not installed" and "the remote refused" are errors; only an
    answer that lists nothing is an empty list.
    """
    base = f"{valid_spec(spec)}/{valid_label(host_label)}"
    quoted = shlex.quote(base)
    if not conn.run("command -v rclone >/dev/null").ok:
        raise BackupError("на сервере не установлен rclone")
    names = conn.run(f"rclone lsf --dirs-only {quoted}/", timeout=300)
    if not names.ok:
        raise BackupError(
            f"rclone не смог прочитать {base}: "
            f"{names.err.strip() or names.out.strip() or 'ошибка'}"
        )
    wanted = [
        n.strip("/ ")
        for n in names.out.split()
        if _SNAPSHOT_NAME.fullmatch(n.strip("/ "))
    ]
    if not wanted:
        return Listing()
    result = conn.run(
        "for n in "
        + " ".join(shlex.quote(n) for n in wanted)
        + f'; do echo "##$n"; rclone cat {quoted}/"$n"/manifest.json 2>/dev/null; echo; done',
        timeout=300,
    )
    return _read_sections(result.out, base)


# ---- making one --------------------------------------------------------------


def dry_run(conn: Connection) -> str:
    result = conn.run("holdfast backup --dry-run", timeout=180)
    if not result.ok:
        raise BackupError(result.err.strip() or result.out.strip() or "dry-run failed")
    return result.out


def run_backup(conn: Connection, on_text: Callable[[str], None]) -> int:
    """`holdfast backup`, output streamed; the exit code is the answer."""
    return conn.run_stream("holdfast backup", on_text, timeout=6 * 3600)


# ---- moving them -------------------------------------------------------------


def pull_to_pc(
    conn: Connection,
    snapshot_dir: str,
    pc_folder: Path,
    host_label: str,
    on_bytes: Callable[[int], None] = lambda n: None,
) -> Path:
    """Copy a server's snapshot to `<pc_folder>/<host_label>/<snapshot>`.

    The connection layer extracts beside the final place and renames when the
    stream ended cleanly, so what is at the destination is whole or absent.
    """
    name = valid_snapshot_name(snapshot_dir.rstrip("/").rpartition("/")[2])
    target = pc_folder / valid_label(host_label)
    if (target / name).exists():
        raise BackupError("this snapshot is already on this computer")
    return conn.download_dir(snapshot_dir, target, on_bytes)


def push_from_pc(
    conn: Connection,
    local_snapshot: Path,
    server_root: str = DEFAULT_ROOT,
    on_bytes: Callable[[int], None] = lambda n: None,
) -> str:
    """Put a snapshot from this computer on the server, whole or not at all.

    It is sent into a hidden staging folder and moved into place only if the
    final name is still free, so a dropped connection leaves nothing that the
    server's retention could mistake for the newest snapshot.
    """
    name = valid_snapshot_name(local_snapshot.name)
    root = server_root.rstrip("/")
    final = f"{root}/{name}"
    if conn.run(f"test -e {shlex.quote(final)}").ok:
        raise BackupError("this snapshot is already on the server")
    staging = f"{root}/.incoming-{secrets.token_hex(4)}"
    try:
        conn.upload_dir(local_snapshot, staging, on_bytes)
        moved = conn.run(
            f"test ! -e {shlex.quote(final)} && "
            f"mv {shlex.quote(f'{staging}/{name}')} {shlex.quote(final)}"
        )
        if not moved.ok:
            raise BackupError("this snapshot is already on the server")
    finally:
        conn.run(f"rm -rf {shlex.quote(staging)}")
    return final


def push_to_remote(
    conn: Connection,
    snapshot_dir: str,
    spec: str,
    host_label: str,
    on_text: Callable[[str], None],
) -> int:
    """Send a server's snapshot to an rclone remote, never deleting there."""
    name = valid_snapshot_name(snapshot_dir.rstrip("/").rpartition("/")[2])
    target = f"{valid_spec(spec)}/{valid_label(host_label)}/{name}"
    return conn.run_stream(
        f"rclone copy {shlex.quote(snapshot_dir)} {shlex.quote(target)} "
        + " ".join(RCLONE_PROGRESS),
        on_text,
        timeout=24 * 3600,
    )


def pull_from_remote(
    conn: Connection,
    spec: str,
    host_label: str,
    name: str,
    server_root: str,
    on_text: Callable[[str], None],
) -> int:
    """Bring a snapshot from a remote onto the server, checked, whole or not at all.

    It is copied into a hidden folder, its SHA256SUMS are verified there, and
    only then is it renamed. A snapshot that already exists is never merged
    into; a copy that fails the check is removed.
    """
    valid_snapshot_name(name)
    source = f"{valid_spec(spec)}/{valid_label(host_label)}/{name}"
    root = server_root.rstrip("/")
    final = f"{root}/{name}"
    part = f"{root}/.{name}.part"
    q = shlex.quote
    code = conn.run_stream(
        f"if [ -e {q(final)} ]; then echo 'этот снимок уже есть на сервере'; exit 17; fi; "
        f"rm -rf {q(part)} && mkdir -p {q(part)}",
        on_text,
        timeout=120,
    )
    if code != 0:
        return code
    code = conn.run_stream(
        f"rclone copy {q(source)} {q(part)} " + " ".join(RCLONE_PROGRESS),
        on_text,
        timeout=24 * 3600,
    )
    if code == 0:
        # A copy that arrived is not a copy that is whole: SHA256SUMS says which.
        code = conn.run_stream(
            f"cd {q(part)} && sha256sum -c SHA256SUMS", on_text, timeout=3600
        )
    if code == 0:
        code = conn.run_stream(
            f"test ! -e {q(final)} && mv {q(part)} {q(final)}", on_text, timeout=120
        )
    if code != 0:
        conn.run(f"rm -rf {q(part)}")
    return code
