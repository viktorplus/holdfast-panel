import json
import uuid

import pytest
from fake_sshd import PASSWORD, FakeSshd, find_bash

from holdfast_panel.ssh import connect, fetch_host_fingerprint
from holdfast_panel.store import Server
from holdfast_panel.templates import (
    MASK,
    TemplateError,
    check_values,
    parse,
    preview,
    render_step,
    run_template,
)

needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs a bash with HOME")


def make(steps, variables=(), **extra):
    return parse(
        {
            "id": "t",
            "name": "T",
            "variables": list(variables),
            "steps": steps,
            **extra,
        }
    )


@pytest.fixture
def conn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sshd = FakeSshd(home)
    server = Server(
        name="t",
        host="127.0.0.1",
        port=sshd.port,
        host_fingerprint=fetch_host_fingerprint("127.0.0.1", sshd.port),
    )
    session = connect(server, password=PASSWORD)
    session.home = home
    yield session
    session.close()
    sshd.close()


# ---- parsing and values ----------------------------------------------------


def test_a_template_with_an_undeclared_variable_is_refused():
    with pytest.raises(TemplateError, match="not declared"):
        make([{"title": "a", "run": "echo {{nope}}"}])


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "Bad Id", "name": "x", "steps": [{"title": "a", "run": "true"}]},
        {"id": "t", "name": "", "steps": [{"title": "a", "run": "true"}]},
        {"id": "t", "name": "x", "steps": []},
        {"id": "t", "name": "x", "steps": [{"title": "a", "run": " "}]},
        {
            "id": "t",
            "name": "x",
            "steps": [{"title": "a", "kind": "file", "path": "rel", "run": "x"}],
        },
        {
            "id": "t",
            "name": "x",
            "variables": [{"name": "A", "label": "l"}],
            "steps": [{"title": "a", "run": "true"}],
        },
    ],
)
def test_malformed_templates_are_refused(bad):
    with pytest.raises(TemplateError):
        parse(bad)


def test_values_are_required_defaulted_and_checked():
    template = make(
        [{"title": "a", "run": "echo {{host}} {{port}}"}],
        [
            {"name": "host", "label": "Host", "pattern": "[a-z.]+"},
            {"name": "port", "label": "Port", "kind": "number", "default": "22"},
        ],
    )
    assert check_values(template, {"host": "a.b"}) == {"host": "a.b", "port": "22"}
    with pytest.raises(TemplateError, match="required"):
        check_values(template, {})
    with pytest.raises(TemplateError, match="does not match the required format"):
        check_values(template, {"host": "A B"})
    with pytest.raises(TemplateError, match="number"):
        check_values(template, {"host": "a", "port": "x"})
    with pytest.raises(TemplateError, match="control character"):
        parse_and_check_newline()


def parse_and_check_newline():
    template = make(
        [{"title": "a", "run": "echo {{v}}"}], [{"name": "v", "label": "V"}]
    )
    check_values(template, {"v": "a\nb"})


def test_a_template_survives_a_json_round_trip():
    template = make(
        [{"title": "a", "run": "echo {{v}}", "dangerous": True}],
        [{"name": "v", "label": "V", "kind": "password"}],
        description="d",
    )
    again = parse(json.loads(template.to_json()))
    assert again == template
    assert again.dangerous


def test_the_preview_shows_commands_and_hides_passwords():
    template = make(
        [
            {"title": "login", "run": "mysql -p{{pw}}"},
            {"title": "conf", "kind": "file", "path": "/etc/x", "run": "pw={{pw}}"},
        ],
        [{"name": "pw", "label": "Password", "kind": "password"}],
    )
    shown = preview(template, {"pw": "s3cret"})
    assert "s3cret" not in json.dumps(shown)
    assert MASK in shown[0][1] and MASK in shown[1][1]


# ---- running ---------------------------------------------------------------


@needs_bash
def test_a_value_can_never_become_a_command(conn):
    evil = "x; echo INJECTED > injected.txt; #"
    template = make(
        [{"title": "echo", "run": "printf %s {{v}}"}], [{"name": "v", "label": "V"}]
    )
    chunks = []
    result = run_template(
        conn, template, {"v": evil}, lambda k, t: chunks.append((k, t))
    )

    assert result.ok
    assert result.outcomes[0].output == evil
    assert not (conn.home / "injected.txt").exists()


@needs_bash
def test_a_file_step_writes_the_content_with_the_mode(conn):
    template = make(
        [
            {
                "title": "write",
                "kind": "file",
                "path": "{{dir}}/app.env",
                "run": 'TOKEN={{token}}\nQUOTE="it\'s"\n',
                "mode": "600",
            }
        ],
        [
            {"name": "dir", "label": "Dir"},
            {"name": "token", "label": "Token"},
        ],
    )
    # An absolute path on the server's side, whichever OS the test runs on.
    folder = f"/tmp/hf-test-{uuid.uuid4().hex}"
    try:
        assert run_template(conn, template, {"dir": folder, "token": "abc"}).ok
        shown = conn.run(f"cat {folder}/app.env")
        assert shown.out == 'TOKEN=abc\nQUOTE="it\'s"\n'
        # chmod itself is not observable on NTFS under Git Bash; the line is.
        line = render_step(template.steps[0], {"dir": folder, "token": "abc"})
        assert line.endswith(f"chmod 600 {folder}/app.env")
    finally:
        conn.run(f"rm -rf {folder}")


@needs_bash
def test_a_password_is_masked_in_the_output_of_the_commands(conn):
    template = make(
        [{"title": "leak", "run": "echo password is {{pw}}"}],
        [{"name": "pw", "label": "P", "kind": "password"}],
    )
    seen = []
    result = run_template(
        conn, template, {"pw": "hunter2"}, lambda k, t: seen.append(t)
    )
    assert "hunter2" not in "".join(seen)
    assert "hunter2" not in result.outcomes[0].output
    assert MASK in result.outcomes[0].output


@needs_bash
def test_the_run_stops_at_the_first_failure(conn):
    template = make(
        [
            {"title": "one", "run": "echo one"},
            {"title": "boom", "run": "echo before; exit 3"},
            {"title": "never", "run": "echo never > never.txt"},
        ]
    )
    result = run_template(conn, template, {})
    assert not result.ok
    assert result.failed_step == "boom"
    assert [o.title for o in result.outcomes] == ["one", "boom"]
    assert result.outcomes[1].code == 3 and "before" in result.outcomes[1].output
    assert not (conn.home / "never.txt").exists()


@needs_bash
def test_a_step_may_be_allowed_to_fail(conn):
    template = make(
        [
            {"title": "optional", "run": "exit 1", "ignore_failure": True},
            {"title": "after", "run": "echo after"},
        ]
    )
    result = run_template(conn, template, {})
    assert result.ok
    assert [o.title for o in result.outcomes] == ["optional", "after"]


def test_a_file_path_that_ends_up_relative_is_refused():
    template = make(
        [{"title": "w", "kind": "file", "path": "{{d}}/x", "run": "x"}],
        [{"name": "d", "label": "D"}],
    )
    with pytest.raises(TemplateError, match="not absolute"):
        render_step(template.steps[0], {"d": "relative"})
