"""Snapshots in the three places they can live, and moving them between them.

* on the server: `backup.root` (default /opt/backups), one folder per run;
* on this computer: `<folder>/<host_label>/<snapshot>/`;
* on a remote storage: an rclone destination, `<spec>/<host_label>/<snapshot>/`,
  reached through a server that has that rclone remote configured.

Snapshots are read from their own `manifest.json`, which is plain JSON, so the
panel does not depend on the holdfast version installed on the server.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .ssh import Connection

DEFAULT_ROOT = "/opt/backups"
_SNAPSHOT_NAME = re.compile(r"^\d{8}-\d{6}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SPEC = re.compile(r"^[A-Za-z0-9_.-]+:[^\s'\"`$\\;|&<>]*$")
MANIFEST_FORMAT = 1


@dataclass(frozen=True)
class Component:
    name: str
    kind: str
    size: int
    target: str = ""


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
        """Folders of compose projects: where `docker compose up` is run."""
        return [c.target for c in self.components if c.name.startswith("project-")]


class BackupError(Exception):
    pass


def valid_snapshot_name(name: str) -> str:
    if not _SNAPSHOT_NAME.match(name):
        raise BackupError(f"{name!r} is not a snapshot name (YYYYMMDD-HHMMSS)")
    return name


def valid_label(label: str) -> str:
    if not _LABEL.match(label):
        raise BackupError(f"{label!r} is not a usable machine name")
    return label


def valid_spec(spec: str) -> str:
    if not _SPEC.match(spec):
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
        components = tuple(
            Component(
                name=str(a.get("component", "")),
                kind=str((a.get("restore") or {}).get("type", "")),
                size=int(a.get("size", 0)),
                target=str((a.get("restore") or {}).get("target", "")),
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
        if not _SNAPSHOT_NAME.match(name):
            continue
        try:
            listing.snapshots.append(parse_manifest(body, f"{base}/{name}"))
        except BackupError as exc:
            listing.problems.append(str(exc))
    listing.snapshots.sort(key=lambda s: s.name, reverse=True)
    return listing


# ---- where snapshots are -----------------------------------------------------


def list_server(conn: Connection, root: str = DEFAULT_ROOT) -> Listing:
    quoted = shlex.quote(root.rstrip("/"))
    result = conn.run(
        f'for d in {quoted}/*/; do n=$(basename "$d"); '
        'if [ -f "$d/manifest.json" ]; then echo "##$n"; cat "$d/manifest.json"; echo; fi; '
        "done",
        timeout=120,
    )
    return _read_sections(result.out, root.rstrip("/"))


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
            if not (snap.is_dir() and manifest.is_file()):
                continue
            try:
                listing.snapshots.append(
                    parse_manifest(manifest.read_text("utf-8"), str(snap))
                )
            except (OSError, BackupError) as exc:
                listing.problems.append(str(exc))
    return listing


def list_remote(conn: Connection, spec: str, host_label: str) -> Listing:
    """Snapshots of one machine on an rclone remote, asked through `conn`."""
    base = f"{valid_spec(spec)}/{valid_label(host_label)}"
    quoted = shlex.quote(base)
    result = conn.run(
        f"for n in $(rclone lsf --dirs-only {quoted}/ 2>/dev/null | tr -d /); do "
        f'echo "##$n"; rclone cat {quoted}/$n/manifest.json 2>/dev/null; echo; done',
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
    """Copy a server's snapshot to `<pc_folder>/<host_label>/<snapshot>`."""
    valid_snapshot_name(snapshot_dir.rstrip("/").rpartition("/")[2])
    target = pc_folder / valid_label(host_label)
    if (target / snapshot_dir.rstrip("/").rpartition("/")[2]).exists():
        raise BackupError("this snapshot is already on this computer")
    return conn.download_dir(snapshot_dir, target, on_bytes)


def push_from_pc(
    conn: Connection,
    local_snapshot: Path,
    server_root: str = DEFAULT_ROOT,
    on_bytes: Callable[[int], None] = lambda n: None,
) -> str:
    valid_snapshot_name(local_snapshot.name)
    existing = conn.run(
        f"test -e {shlex.quote(f'{server_root}/{local_snapshot.name}')}"
    )
    if existing.ok:
        raise BackupError("this snapshot is already on the server")
    return conn.upload_dir(local_snapshot, server_root, on_bytes)


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
        f"rclone copy {shlex.quote(snapshot_dir)} {shlex.quote(target)} --progress "
        "--stats-one-line",
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
    """Bring a snapshot from a remote onto the server, then check its checksums."""
    valid_snapshot_name(name)
    source = f"{valid_spec(spec)}/{valid_label(host_label)}/{name}"
    target = f"{server_root.rstrip('/')}/{name}"
    code = conn.run_stream(
        f"mkdir -p {shlex.quote(target)} && "
        f"rclone copy {shlex.quote(source)} {shlex.quote(target)} --progress "
        "--stats-one-line",
        on_text,
        timeout=24 * 3600,
    )
    if code != 0:
        return code
    # A copy that arrived is not a copy that is whole: SHA256SUMS says which.
    return conn.run_stream(
        f"cd {shlex.quote(target)} && sha256sum -c SHA256SUMS", on_text, timeout=3600
    )
