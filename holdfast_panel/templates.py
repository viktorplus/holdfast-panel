"""Templates: a named list of shell steps with variables.

A template is data (JSON), so it can be edited in the panel, copied and shared.
Two safety rules hold everywhere:

* a variable placed in a shell step is always quoted with `shlex.quote`, so a
  value can never become a command;
* a variable of kind "password" is masked in everything the panel shows or
  logs, including the preview and the output of the commands.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ssh import Connection

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
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


def parse(raw: Any, builtin: bool = False) -> Template:
    """Build a template from loaded JSON, refusing anything malformed."""
    if not isinstance(raw, dict):
        raise TemplateError("a template is a JSON object")
    tid = str(raw.get("id", ""))
    if not _ID.match(tid):
        raise TemplateError(f"id {tid!r}: lowercase letters, digits, - and _ only")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise TemplateError("the template has no name")

    variables = []
    for item in raw.get("variables", []):
        try:
            var = Variable(**item)
        except TypeError as exc:
            raise TemplateError(f"variable {item!r}: {exc}") from exc
        if not _NAME.match(var.name):
            raise TemplateError(f"variable name {var.name!r}: a-z, 0-9 and _ only")
        if var.kind not in KINDS:
            raise TemplateError(f"variable {var.name}: kind must be one of {KINDS}")
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
    for item in raw.get("steps", []):
        try:
            step = Step(**item)
        except TypeError as exc:
            raise TemplateError(f"step {item!r}: {exc}") from exc
        if not step.title.strip():
            raise TemplateError("a step has no title")
        if step.kind not in STEP_KINDS:
            raise TemplateError(f"step {step.title!r}: kind must be {STEP_KINDS}")
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


def check_values(template: Template, values: dict[str, str]) -> dict[str, str]:
    """The values to use: defaults filled in, each one validated."""
    final: dict[str, str] = {}
    for var in template.variables:
        value = str(values.get(var.name, var.default))
        if not value:
            if var.required:
                raise TemplateError(f"{var.label}: a value is required")
            final[var.name] = ""
            continue
        if var.kind == "number" and not value.isdigit():
            raise TemplateError(f"{var.label}: a number is required")
        if var.pattern and not re.fullmatch(var.pattern, value):
            raise TemplateError(f"{var.label}: {value!r} does not look right")
        if "\n" in value or "\r" in value or "\0" in value:
            raise TemplateError(f"{var.label}: line breaks are not allowed")
        final[var.name] = value
    return final


def _fill(text: str, values: dict[str, str], quote: bool) -> str:
    def put(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        return shlex.quote(value) if quote else value

    return _PLACEHOLDER.sub(put, text)


def render_step(step: Step, values: dict[str, str]) -> str:
    """The exact shell line that will run for this step."""
    if step.kind == "file":
        if not _fill(step.path, values, quote=False).startswith("/"):
            raise TemplateError(f"step {step.title!r}: the path is not absolute")
        path = _fill(step.path, values, quote=True)
        content = _fill(step.run, values, quote=False)
        data = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return (
            f'mkdir -p "$(dirname {path})" && '
            f"echo {data} | base64 -d > {path} && chmod {step.mode} {path}"
        )
    return f"bash -o pipefail -c {shlex.quote(_fill(step.run, values, quote=True))}"


def secrets_of(template: Template, values: dict[str, str]) -> list[str]:
    return [
        values[v.name]
        for v in template.variables
        if v.kind == "password" and values[v.name]
    ]


def mask(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


def preview(template: Template, values: dict[str, str]) -> list[tuple[str, str]]:
    """(title, what would run) for every step, passwords masked. Runs nothing."""
    values = check_values(template, values)
    secrets = secrets_of(template, values)
    out = []
    for step in template.steps:
        if step.kind == "file":
            shown = f"write {_fill(step.path, values, quote=False)}:\n" + _fill(
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


def run_template(
    conn: Connection,
    template: Template,
    values: dict[str, str],
    on_event: Callable[[str, str], None] = lambda kind, text: None,
) -> RunResult:
    """Run the steps in order, stopping at the first failure.

    `on_event(kind, text)` gets kind "step" (a title), "output" (masked text) and
    "result" (a one-line summary). A step marked `ignore_failure` does not stop
    the run.
    """
    values = check_values(template, values)
    secrets = secrets_of(template, values)
    result = RunResult(template.id)
    for step in template.steps:
        on_event("step", step.title)
        collected: list[str] = []

        def sink(text: str, collected=collected) -> None:
            masked = mask(text, secrets)
            collected.append(masked)
            on_event("output", masked)

        code = conn.run_stream(render_step(step, values), sink, timeout=step.timeout)
        outcome = StepOutcome(step.title, code, "".join(collected))
        result.outcomes.append(outcome)
        if code != 0:
            on_event("result", f"{step.title}: exit {code}")
            if not step.ignore_failure:
                break
            outcome.skipped = True
        else:
            on_event("result", f"{step.title}: ok")
    return result
