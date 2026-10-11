"""Editing /etc/holdfast/holdfast.toml from the panel.

The file is edited as text, not parsed and rewritten: a round trip through a
TOML library would drop the user's comments and reorder their keys. Every edit
touches only the lines of one key, and the result is parsed again to prove that
the file is still valid TOML and says what was meant.
"""

from __future__ import annotations

import difflib
import json
import re
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass

from .ssh import Connection

CONFIG_PATH = "/etc/holdfast/holdfast.toml"
_EXCLUDE = re.compile(r"^(volume|path|database|container):(.+)$")
_RECIPIENT = re.compile(r"^age1[a-z0-9]+$")


class ConfigEditError(Exception):
    pass


def toml_value(value: str | list[str]) -> str:
    if isinstance(value, list):
        if not value:
            return "[]"
        inner = "".join(f"  {json.dumps(v, ensure_ascii=False)},\n" for v in value)
        return "[\n" + inner + "]"
    return json.dumps(value, ensure_ascii=False)


def set_section_key(text: str, section: str, key: str, value: str | list[str]) -> str:
    """Set `key` inside `[section]`, leaving every other line as it was.

    A file with Windows line endings keeps them: the new lines get the same.
    """
    crlf = "\r\n" in text
    new = _set_section_key(text.replace("\r\n", "\n"), section, key, value)
    return new.replace("\n", "\r\n") if crlf else new


def _set_section_key(text: str, section: str, key: str, value: str | list[str]) -> str:
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"the file is not valid TOML as it is: {exc}") from exc
    lines = text.split("\n")
    header = re.compile(rf"^\[{re.escape(section)}\]\s*(#.*)?$")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    rendered = f"{key} = {toml_value(value)}".split("\n")

    if start is None:
        body = text.rstrip("\n")
        sep = "\n\n" if body else ""
        new = f"{body}{sep}[{section}]\n" + "\n".join(rendered) + "\n"
    else:
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")
            ),
            len(lines),
        )
        found = next(
            (
                i
                for i in range(start + 1, end)
                if re.match(rf"^{re.escape(key)}\s*=", lines[i])
            ),
            None,
        )
        if found is None:
            lines[start + 1 : start + 1] = rendered
        else:
            last = found
            if re.match(rf"^{re.escape(key)}\s*=\s*\[\s*(#.*)?$", lines[found]):
                # A multi-line array: it ends at the first line that closes it.
                last = next(
                    (
                        i
                        for i in range(found + 1, end)
                        if lines[i].split("#")[0].strip().endswith("]")
                    ),
                    end - 1,
                )
            lines[found : last + 1] = rendered
        new = "\n".join(lines)

    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"the edit would make the file invalid: {exc}") from exc
    if parsed.get(section, {}).get(key) != value:
        raise ConfigEditError(
            f"could not set {section}.{key} safely; edit the file by hand"
        )
    # The proof is not only that the key says what was meant: nothing else moved.
    for tree in (before, parsed):
        tree.get(section, {}).pop(key, None)
        if tree.get(section) == {}:
            del tree[
                section
            ]  # a section made only for this key is not "something else"
    if before != parsed:
        raise ConfigEditError(
            f"the edit of {section}.{key} would change something else; "
            "edit the file by hand"
        )
    return new


def set_backup_mode(text: str, mode: str) -> str:
    if mode not in ("auto", "manual"):
        raise ConfigEditError("the mode is auto or manual")
    return set_section_key(text, "backup", "mode", mode)


def set_excludes(text: str, items: list[str]) -> str:
    for item in items:
        problem = exclude_problem(item)
        if problem:
            raise ConfigEditError(problem)
    return set_section_key(text, "backup", "exclude", items)


def exclude_problem(item: str) -> str:
    match = _EXCLUDE.match(item)
    if not match:
        return f"{item!r}: expected volume:<name>, path:</abs/path>, database:<container>/<db> or container:<name>"
    kind, rest = match.groups()
    if kind == "path" and not rest.startswith("/"):
        return f"{item!r}: a path must start with /"
    if kind == "database" and "/" not in rest.strip("/"):
        return f"{item!r}: write database:<container>/<database>"
    return ""


def problems(text: str) -> list[str]:
    """What is wrong with this file, found without a server."""
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"not valid TOML: {exc}"]
    found = []
    backup = config.get("backup", {})
    if backup.get("mode", "auto") not in ("auto", "manual"):
        found.append(f"backup.mode is {backup['mode']!r}; it is auto or manual")
    for item in backup.get("exclude", []):
        problem = exclude_problem(str(item))
        if problem:
            found.append(f"backup.exclude: {problem}")
    for i, component in enumerate(config.get("component", []), 1):
        if not component.get("type") or not component.get("name"):
            found.append(f"[[component]] number {i} needs type and name")
    for recipient in config.get("encryption", {}).get("recipients", []):
        if not _RECIPIENT.match(str(recipient)):
            found.append(
                f"encryption.recipients: {recipient!r} is not an age public key"
            )
    return found


def diff(old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        "holdfast.toml (сейчас)",
        "holdfast.toml (новый)",
        lineterm="",
    )
    return "\n".join(lines)


def read_config(conn: Connection, path: str = CONFIG_PATH) -> str:
    result = conn.run(f"cat {shlex.quote(path)}")
    if not result.ok:
        raise ConfigEditError(f"cannot read {path}: {result.err.strip()}")
    return result.out


@dataclass
class SaveResult:
    backup_path: str
    dry_run: str
    rolled_back: bool = False
    rollback_failed: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return not (self.rolled_back or self.rollback_failed)


def save_config(
    conn: Connection,
    new_text: str,
    *,
    expected: str | None = None,
    path: str = CONFIG_PATH,
    on_text: Callable[[str], None] = lambda text: None,
) -> SaveResult:
    """Write the file, keeping the old one, and undo it if the backup stops working.

    `expected` is the text the edit started from: if the file on the server is
    different now (someone, or another panel, changed it), nothing is written.
    A copy `holdfast.toml.bak-<time>` is made first and keeps the original mode
    and owner. After the write, `backup --dry-run` must still work *if it worked
    before*: a server whose dry-run was already failing is not blamed on this
    edit. "Works" is only "does not stop with an error": holdfast prints
    problems it would refuse over as warnings, so read the plan afterwards.
    """
    found = problems(new_text)
    if found:
        raise ConfigEditError("; ".join(found))
    if expected is not None and read_config(conn, path) != expected:
        raise ConfigEditError(
            "the file on the server has changed since it was loaded; load it "
            "again and repeat the edit (nothing was written)"
        )

    q = shlex.quote
    worked_before = conn.run("holdfast backup --dry-run", timeout=180).ok
    stamp = conn.run("date +%Y%m%d-%H%M%S-%N").out.strip()
    backup = f"{path}.bak-{stamp}"
    if not conn.run(f"cp -p {q(path)} {q(backup)}").ok:
        raise ConfigEditError(f"could not make a copy of {path}; nothing was changed")

    staged = f"{path}.new"
    try:
        conn.put_bytes(staged, new_text.encode("utf-8"), mode="600")
        # the new file is made like the old one: same mode, same owner
        conn.run(f"chmod --reference={q(backup)} {q(staged)}")
        conn.run(f"chown --reference={q(backup)} {q(staged)}")
        replaced = conn.run(f"mv -f {q(staged)} {q(path)}")
    except Exception:
        conn.run(f"rm -f {q(staged)}")
        raise
    if not replaced.ok:
        conn.run(f"rm -f {q(staged)}")
        raise ConfigEditError(f"could not replace {path}; the old file is untouched")

    dry = conn.run("holdfast backup --dry-run", timeout=180)
    on_text(dry.out)
    if worked_before and not dry.ok:
        reason = dry.err.strip() or dry.out
        if conn.run(f"cp -p {q(backup)} {q(path)}").ok:
            return SaveResult(
                backup,
                reason,
                rolled_back=True,
                message="после правки бэкап перестал запускаться; файл возвращён как был",
            )
        return SaveResult(
            backup,
            reason,
            rollback_failed=True,
            message=f"бэкап перестал запускаться, а вернуть прежний файл не удалось: "
            f"копия лежит в {backup}",
        )
    return SaveResult(backup, dry.out, message="сохранено")
