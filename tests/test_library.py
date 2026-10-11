import subprocess

import pytest
from fake_sshd import find_bash

from holdfast_panel.builtin_templates import builtin_templates
from holdfast_panel.library import Library
from holdfast_panel.templates import (
    TemplateError,
    check_values,
    parse,
    preview,
    render_step,
)

SAMPLES = {
    "host_label": "web-1",
    "recipient": "age1" + "q" * 58,
    "project": "myapp",
    "db_password": "Passw0rd-123",
    "root_password": "Rootp4ss-123",
    "password": "Passw0rd-123",
    "ssh_port": "2222",
}


def sample_values(template):
    return {v.name: SAMPLES[v.name] for v in template.variables if v.name in SAMPLES}


def test_every_builtin_parses_previews_and_hides_its_passwords():
    for template in builtin_templates():
        values = sample_values(template)
        shown = preview(template, values)
        assert len(shown) == len(template.steps)
        text = str(shown)
        for var in template.variables:
            if var.kind == "password":
                assert SAMPLES[var.name] not in text, template.id


@pytest.mark.skipif(find_bash() is None, reason="needs bash")
def test_every_shell_line_of_every_builtin_is_valid_bash():
    for template in builtin_templates():
        values = sample_values(template)
        # check_values fills defaults the same way a real run does
        filled = check_values(template, values)
        for step in template.steps:
            line = render_step(step, filled)
            done = subprocess.run(
                [find_bash(), "-n", "-c", line],
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode == 0, (template.id, step.title, done.stderr)


def test_the_bootstrap_cannot_take_a_malformed_key_or_label():
    bootstrap = next(t for t in builtin_templates() if t.id == "holdfast-bootstrap")
    good = sample_values(bootstrap)
    check_values(bootstrap, good)
    for field, bad in (
        ("recipient", "age1abc'; reboot; '"),
        ("host_label", "web 1; reboot"),
        ("backup_mode", "sometimes"),
    ):
        with pytest.raises(TemplateError):
            check_values(bootstrap, {**good, field: bad})


def test_the_library_keeps_a_users_template(tmp_path):
    library = Library(tmp_path / "templates")
    template = parse(
        {"id": "mine", "name": "Mine", "steps": [{"title": "a", "run": "true"}]}
    )
    library.save(template)

    again = Library(tmp_path / "templates")
    assert again.get("mine") == template
    assert [t.id for t in again.all()][-1] == "mine"
    assert len(again.all()) == len(builtin_templates()) + 1

    again.delete("mine")
    assert Library(tmp_path / "templates").problems() == []
    with pytest.raises(TemplateError):
        again.get("mine")


def test_a_builtin_cannot_be_overwritten_or_deleted(tmp_path):
    library = Library(tmp_path)
    template = builtin_templates()[0]
    with pytest.raises(TemplateError, match="built-in"):
        library.save(template)
    with pytest.raises(TemplateError):
        library.delete(template.id)


def test_a_broken_user_file_is_reported_and_does_not_hide_the_rest(tmp_path):
    library = Library(tmp_path)
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    library.import_text(
        '{"id": "ok", "name": "Ok", "steps": [{"title": "a", "run": "true"}]}'
    )

    assert "ok" in [t.id for t in library.all()]
    assert any(p.startswith("bad.json") for p in library.problems())


def test_pasted_text_that_is_not_a_template_is_refused(tmp_path):
    with pytest.raises(TemplateError):
        Library(tmp_path).import_text("[1, 2]")
    with pytest.raises(TemplateError, match="JSON"):
        Library(tmp_path).import_text("{")


@pytest.mark.skipif(find_bash() is None, reason="needs bash")
def test_the_key_step_really_edits_the_recipients_line(tmp_path):
    bootstrap = next(t for t in builtin_templates() if t.id == "holdfast-bootstrap")
    step = next(s for s in bootstrap.steps if "ключ шифрования" in s.title)
    config = tmp_path / "holdfast.toml"
    config.write_text('host_label = "web-1"\n\n[encryption]\nrecipients = []\n')
    line = render_step(step, check_values(bootstrap, sample_values(bootstrap)))
    line = line.replace("/etc/holdfast/holdfast.toml", config.as_posix())

    done = subprocess.run(
        [find_bash(), "-c", line], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr
    assert (
        config.read_text().splitlines()[-1]
        == f'recipients = ["{SAMPLES["recipient"]}"]'
    )
    assert 'host_label = "web-1"' in config.read_text()
