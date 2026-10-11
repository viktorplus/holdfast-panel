"""Built-in templates plus the user's own, which live as JSON files."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .builtin_templates import builtin_templates
from .templates import Template, TemplateError, parse


class Library:
    def __init__(self, user_dir: Path):
        self.user_dir = user_dir

    def _path(self, template_id: str) -> Path:
        return self.user_dir / f"{template_id}.json"

    def all(self) -> list[Template]:
        """Built-ins first, then the user's; a user file that is broken is
        reported in `problems()` instead of hiding the rest."""
        return [*builtin_templates(), *self._user()[0]]

    def problems(self) -> list[str]:
        return self._user()[1]

    def _user(self) -> tuple[list[Template], list[str]]:
        found: list[Template] = []
        problems: list[str] = []
        seen: dict[str, str] = {}
        if self.user_dir.is_dir():
            for path in sorted(self.user_dir.glob("*.json")):
                try:
                    template = parse(json.loads(path.read_text("utf-8")))
                except (ValueError, OSError, TemplateError) as exc:
                    problems.append(f"{path.name}: {exc}")
                    continue
                if template.id != path.stem:
                    problems.append(
                        f"{path.name}: the id is {template.id!r}, so it cannot be "
                        "deleted or told apart; rename the file to match"
                    )
                    continue
                if template.id in seen or any(
                    template.id == b.id for b in builtin_templates()
                ):
                    problems.append(
                        f"{path.name}: the id {template.id!r} is already taken"
                    )
                    continue
                seen[template.id] = path.name
                found.append(template)
        return found, problems

    def get(self, template_id: str) -> Template:
        for template in self.all():
            if template.id == template_id:
                return template
        raise TemplateError(f"no template called {template_id!r}")

    def save(self, template: Template) -> None:
        if any(b.id == template.id for b in builtin_templates()):
            raise TemplateError(
                f"{template.id!r} is a built-in template; save it under another id"
            )
        # Validated by a round trip: what is saved is what `parse` accepts.
        parse(json.loads(template.to_json()))
        self.user_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(template.id).with_suffix(".tmp")
        tmp.write_text(template.to_json(), encoding="utf-8")
        os.replace(tmp, self._path(template.id))

    def delete(self, template_id: str) -> None:
        path = self._path(template_id)
        if not path.is_file():
            raise TemplateError(f"{template_id!r} is not one of your templates")
        path.unlink()

    def import_text(self, text: str, replace: str | None = None) -> Template:
        """Add a template from pasted JSON.

        An id that is already one of yours is refused, so a paste cannot quietly
        overwrite a template; editing passes `replace` with the id being edited.
        """
        try:
            template = parse(json.loads(text))
        except ValueError as exc:
            raise TemplateError(f"not valid JSON: {exc}") from exc
        if self._path(template.id).exists() and template.id != replace:
            raise TemplateError(
                f"you already have a template with the id {template.id!r}; "
                "change the id, or edit the existing one"
            )
        self.save(template)
        return template
