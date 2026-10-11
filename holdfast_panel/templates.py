"""Templates: a named list of shell steps with variables.

A template is data (JSON), so it can be edited in the panel, copied and shared.
Two safety rules hold everywhere:

* a variable placed in a shell step is always quoted with `shlex.quote`, so a
  value can never become a command;
* a variable of kind "password" is masked in everything the panel shows or
  logs, including the preview and the output of the commands.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ssh import Connection, SshError

_NAME = re.compile(r"[a-z][a-z0-9_]*")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_MODE = re.compile(r"[0-7]{3,4}")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
KINDS = ("text", "password", "number")
STEP_KINDS = ("shell", "file")
MASK = "••••••"


class TemplateError(Exception):
    pass


@dataclass(frozen=True)
class Variable:
    name: str
    label: str
    kind: str = "text"
    default: str = ""
    required: bool = True
    pattern: str = ""
    """A regular expression the whole value must match; empty means anything."""


@dataclass(frozen=True)
class Step:
    title: str
    run: str = ""
    kind: str = "shell"
    path: str = ""
    """For kind "file": where to write `run` (the file's content)."""
    mode: str = "644"
    ignore_failure: bool = False
    dangerous: bool = False
    timeout: int = 1800


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    description: str = ""
    category: str = "custom"
    variables: tuple[Variable, ...] = ()
    steps: tuple[Step, ...] = ()
    builtin: bool = False

    @property
    def dangerous(self) -> bool:
        return any(s.dangerous for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "variables": [_clean(v.__dict__, Variable) for v in self.variables],
            "steps": [_clean(s.__dict__, Step) for s in self.steps],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def _clean(data: dict[str, Any], cls: type) -> dict[str, Any]:
    """Only what differs from the defaults, so a saved file stays readable."""
    defaults = cls.__dataclass_fields__
    return {
        k: v
        for k, v in data.items()
        if k in ("name", "label", "title") or v != defaults[k].default
    }


def _outside_quotes(text: str, start: int) -> bool:
    """Is position `start` of a shell line outside every quote?

    A small scanner for the quoting a person writes by hand: single quotes,
    double quotes, backslash. It does not try to be bash; it only has to notice
    a placeholder written between quotes.
    """
    single = double = escaped = False
    for char in text[:start]:
        if escaped:
            escaped = False
        elif char == "\\" and not single:
            escaped = True
        elif char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
    return not (single or double)


def _check_step_fields(step: Step) -> None:
    """Types and shapes of a step: a JSON file may hold anything."""
    title = step.title
    if not isinstance(title, str) or not title.strip():
        raise TemplateError("a step has no title")
    for name in ("run", "kind", "path", "mode"):
        if not isinstance(getattr(step, name), str):
            raise TemplateError(f"step {title!r}: {name} must be text")
    for name in ("ignore_failure", "dangerous"):
        if not isinstance(getattr(step, name), bool):
            raise TemplateError(f"step {title!r}: {name} must be true or false")
    if (
        not isinstance(step.timeout, int)
        or isinstance(step.timeout, bool)
        or step.timeout <= 0
    ):
        raise TemplateError(f"step {title!r}: timeout must be a positive number")
    if step.kind not in STEP_KINDS:
        raise TemplateError(f"step {title!r}: kind must be {STEP_KINDS}")
    if step.kind == "file" and not _MODE.fullmatch(step.mode):
        raise TemplateError(f"step {title!r}: mode is like 644 or 0600")


def parse(raw: Any, builtin: bool = False) -> Template:
    """Build a template from loaded JSON, refusing anything malformed.

    Whatever shape the JSON has, the result is a Template or a TemplateError:
    a damaged file must not take the whole library down with a TypeError.
    """
    try:
        return _parse(raw, builtin)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:
        raise TemplateError(f"the template is malformed: {exc}") from exc


def _parse(raw: Any, builtin: bool) -> Template:
    if not isinstance(raw, dict):
        raise TemplateError("a template is a JSON object")
    tid = raw.get("id", "")
    if not isinstance(tid, str) or not _ID.fullmatch(tid):
        raise TemplateError(f"id {tid!r}: lowercase letters, digits, - and _ only")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise TemplateError("the template has no name")
    raw_variables, raw_steps = raw.get("variables", []), raw.get("steps", [])
    if not isinstance(raw_variables, list) or not isinstance(raw_steps, list):
        raise TemplateError("variables and steps are lists")

    variables = []
    for item in raw_variables:
        try:
            var = Variable(**item)
        except TypeError as exc:
            raise TemplateError(f"variable {item!r}: {exc}") from exc
        if not isinstance(var.name, str) or not _NAME.fullmatch(var.name):
            raise TemplateError(f"variable name {var.name!r}: a-z, 0-9 and _ only")
        if not isinstance(var.label, str) or not isinstance(var.default, str):
            raise TemplateError(f"variable {var.name}: label and default are text")
        if var.kind not in KINDS:
            raise TemplateError(f"variable {var.name}: kind must be one of {KINDS}")
        if not isinstance(var.required, bool):
            raise TemplateError(f"variable {var.name}: required is true or false")
        if not isinstance(var.pattern, str):
            raise TemplateError(f"variable {var.name}: pattern is text")
        if var.pattern:
            try:
                re.compile(var.pattern)
            except re.error as exc:
                raise TemplateError(f"variable {var.name}: bad pattern: {exc}") from exc
        variables.append(var)
    names = [v.name for v in variables]
    if len(set(names)) != len(names):
        raise TemplateError("two variables have the same name")

    steps = []
    for item in raw_steps:
        try:
            step = Step(**item)
        except TypeError as exc:
            raise TemplateError(f"step {item!r}: {exc}") from exc
        _check_step_fields(step)
        if step.kind == "shell" and not step.run.strip():
            raise TemplateError(f"step {step.title!r} has no command")
        if step.kind == "file" and not step.path.startswith(("/", "{{")):
            raise TemplateError(
                f"step {step.title!r}: path must be absolute or start with a variable"
            )
        for used in _PLACEHOLDER.findall(step.run + step.path):
            if used not in names:
                raise TemplateError(
                    f"step {step.title!r} uses {{{{{used}}}}}, which is not declared"
                )
        if step.kind == "shell":
            for match in _PLACEHOLDER.finditer(step.run):
                if not _outside_quotes(step.run, match.start()):
                    raise TemplateError(
                        f"step {step.title!r}: {{{{{match.group(1)}}}}} is inside "
                        "quotes. Values are quoted for you; write it without quotes"
                    )
        steps.append(step)
    if not steps:
        raise TemplateError("the template has no steps")

    return Template(
        id=tid,
        name=name,
        description=str(raw.get("description", "")),
        category=str(raw.get("category", "custom")),
        variables=tuple(variables),
        steps=tuple(steps),
        builtin=builtin,
    )


MIN_PASSWORD = 4
"""Shorter secrets cannot be masked without mangling everything else."""


def _forbidden_character(value: str) -> str | None:
    for char in value:
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            return f"U+{ord(char):04X}"
    return None


def check_values(template: Template, values: dict[str, str]) -> dict[str, str]:
    """The values to use: defaults filled in, each one validated.

    An error never repeats the value: it may be a password that was nearly
    right, and these messages are shown and written to the action log.
    """
    final: dict[str, str] = {}
    for var in template.variables:
        value = str(values.get(var.name, var.default))
        if not value:
            if var.required:
                raise TemplateError(f"{var.label}: a value is required")
            final[var.name] = ""
            continue
        bad = _forbidden_character(value)
        if bad:
            raise TemplateError(
                f"{var.label}: a control character ({bad}) is not allowed"
            )
        if var.kind == "number" and not (value.isascii() and value.isdigit()):
            raise TemplateError(f"{var.label}: a number is required")
        if var.kind == "password" and len(value) < MIN_PASSWORD:
            raise TemplateError(f"{var.label}: at least {MIN_PASSWORD} characters")
        if var.pattern and not re.fullmatch(var.pattern, value):
            raise TemplateError(
                f"{var.label}: the value does not match the required format"
            )
        final[var.name] = value
    return final


def _fill(text: str, values: dict[str, str], quote: bool) -> str:
    def put(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        return shlex.quote(value) if quote else value

    return _PLACEHOLDER.sub(put, text)


def _file_target(step: Step, values: dict[str, str]) -> str:
    path = _fill(step.path, values, quote=False)
    if not path.startswith("/"):
        raise TemplateError(f"step {step.title!r}: the path is not absolute")
    return path


def render_step(step: Step, values: dict[str, str]) -> str:
    """The shell line that will run for this step.

    For a file step this is the command that prepares and closes the file; the
    content itself travels over the channel (see `run_template`), never on a
    command line where `ps` on the server would show it.
    """
    if step.kind == "file":
        path = shlex.quote(_file_target(step, values))
        return (
            f'mkdir -p "$(dirname {path})" && umask 077 && cat > {path} && '
            f"chmod {step.mode} {path}"
        )
    return f"bash -o pipefail -c {shlex.quote(_fill(step.run, values, quote=True))}"


def secrets_of(template: Template, values: dict[str, str]) -> list[str]:
    return [
        values[v.name]
        for v in template.variables
        if v.kind == "password" and values[v.name]
    ]


def _forms(secrets: list[str]) -> list[str]:
    """Every spelling of a secret that can show up: as typed, and as quoted."""
    found = {s for s in secrets if len(s) >= MIN_PASSWORD}
    found |= {shlex.quote(s) for s in list(found)}
    return sorted(found, key=len, reverse=True)


def _pattern(secrets: list[str]) -> re.Pattern[str] | None:
    forms = _forms(secrets)
    return re.compile("|".join(re.escape(s) for s in forms)) if forms else None


def mask(text: str, secrets: list[str]) -> str:
    pattern = _pattern(secrets)
    return pattern.sub(MASK, text) if pattern else text


class Masker:
    """Masks secrets in text that arrives in pieces.

    A secret split across two reads (`hunte` + `r2`) is in neither piece. So the
    last `longest - 1` characters are held back until the next piece shows
    whether they begin a secret; `flush` releases the rest at the end.
    """

    def __init__(self, secrets: list[str]):
        self._pattern = _pattern(secrets)
        forms = _forms(secrets)
        self._hold = max((len(f) for f in forms), default=1) - 1
        self._buffer = ""

    def feed(self, text: str) -> str:
        if self._pattern is None:
            return text
        self._buffer += text
        cut = max(0, len(self._buffer) - self._hold)
        for match in self._pattern.finditer(self._buffer):
            if match.start() < cut < match.end():
                cut = match.end()  # never cut through a secret
        emit, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._pattern.sub(MASK, emit)

    def flush(self) -> str:
        rest, self._buffer = self._buffer, ""
        return self._pattern.sub(MASK, rest) if self._pattern else rest


def preview(template: Template, values: dict[str, str]) -> list[tuple[str, str]]:
    """(title, what would run) for every step, passwords masked. Runs nothing."""
    values = check_values(template, values)
    secrets = secrets_of(template, values)
    out = []
    for step in template.steps:
        if step.kind == "file":
            shown = f"write {_file_target(step, values)} (mode {step.mode}):\n" + _fill(
                step.run, values, quote=False
            )
        else:
            shown = _fill(step.run, values, quote=True)
        out.append((step.title, mask(shown, secrets)))
    return out


@dataclass
class StepOutcome:
    title: str
    code: int
    output: str = ""
    skipped: bool = False
    """True when the step failed and the template said that is fine."""


@dataclass
class RunResult:
    template: str
    outcomes: list[StepOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.code == 0 or o.skipped for o in self.outcomes)

    @property
    def failed_step(self) -> str | None:
        for o in self.outcomes:
            if o.code != 0 and not o.skipped:
                return o.title
        return None


def _run_file_step(conn: Connection, step: Step, values: dict[str, str]) -> int:
    path = _file_target(step, values)
    directory = posixpath.dirname(path) or "/"
    made = conn.run(f"mkdir -p {shlex.quote(directory)}")
    if not made.ok:
        return made.code
    conn.put_bytes(
        path, _fill(step.run, values, quote=False).encode("utf-8"), step.mode
    )
    return 0


def run_template(
    conn: Connection,
    template: Template,
    values: dict[str, str],
    on_event: Callable[[str, str], None] = lambda kind, text: None,
) -> RunResult:
    """Run the steps in order, stopping at the first failure.

    `on_event(kind, text)` gets kind "step" (a title), "output" (masked text) and
    "result" (a one-line summary). A step marked `ignore_failure` does not stop
    the run. If the connection fails in the middle, what the earlier steps did
    is kept and the step that was running is the failed one.
    """
    values = check_values(template, values)
    secrets = secrets_of(template, values)
    result = RunResult(template.id)
    for step in template.steps:
        on_event("step", step.title)
        collected: list[str] = []
        masker = Masker(secrets)

        def sink(text: str, collected=collected, masker=masker) -> None:
            safe = masker.feed(text)
            if safe:
                collected.append(safe)
                on_event("output", safe)

        try:
            if step.kind == "file":
                code = _run_file_step(conn, step, values)
            else:
                code = conn.run_stream(
                    render_step(step, values), sink, timeout=step.timeout
                )
        except (SshError, OSError, EOFError) as exc:
            on_event("result", f"{step.title}: {exc}")
            result.outcomes.append(StepOutcome(step.title, -1, f"{exc}"))
            break
        rest = masker.flush()
        if rest:
            collected.append(rest)
            on_event("output", rest)
        outcome = StepOutcome(step.title, code, "".join(collected))
        result.outcomes.append(outcome)
        if code != 0:
            outcome.skipped = step.ignore_failure
            on_event("result", f"{step.title}: exit {code}")
            if not step.ignore_failure:
                break
        else:
            on_event("result", f"{step.title}: ok")
    return result
