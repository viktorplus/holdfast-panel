"""Findings of the review of the template engine, each with a test that fails on
the code before the fix."""

import json
import subprocess

import pytest
from fake_sshd import find_bash

from holdfast_panel import actionlog
from holdfast_panel.builtin_templates import builtin_templates
from holdfast_panel.group import run_on_servers
from holdfast_panel.library import Library
from holdfast_panel.ssh import Result, SshError
from holdfast_panel.store import Server
from holdfast_panel.templates import (
    MASK,
    Masker,
    TemplateError,
    check_values,
    mask,
    parse,
    preview,
    render_step,
    run_template,
)

needs_bash = pytest.mark.skipif(find_bash() is None, reason="needs bash")


def template(steps, variables=()):
    return parse({"id": "t", "name": "T", "variables": list(variables), "steps": steps})


def one(run, kind="text"):
    return template(
        [{"title": "a", "run": run}], [{"name": "v", "label": "V", "kind": kind}]
    )


# ---- a placeholder between quotes would become a command ------------------------------------


@pytest.mark.parametrize(
    "run",
    [
        'echo "{{v}}"',
        "echo '{{v}}'",
        "echo \"a '{{v}}' b\"",
        "echo 'it'\"s {{v}}\"",
        'echo foo"{{v}}"',
    ],
)
def test_a_placeholder_inside_quotes_is_refused_when_the_template_is_read(run):
    with pytest.raises(TemplateError, match="inside quotes"):
        one(run)


@pytest.mark.parametrize(
    "run",
    [
        "echo {{v}}",
        'echo "a b" {{v}}',
        "echo '$HOME' {{v}} \"done\"",
        'echo \\" {{v}}',
        "echo a'b'{{v}}",
    ],
)
def test_a_placeholder_outside_quotes_is_fine(run):
    one(run)


# ---- what an error says ---------------------------------------------------------------------------


def test_an_error_never_repeats_the_value():
    pw = template(
        [{"title": "a", "run": "echo {{pw}}"}],
        [
            {
                "name": "pw",
                "label": "Password",
                "kind": "password",
                "pattern": "[a-z]{12}",
            }
        ],
    )
    for value in ("short1", "Nearly-Right-123"):
        with pytest.raises(TemplateError) as caught:
            check_values(pw, {"pw": value})
        assert value not in str(caught.value)


@pytest.mark.parametrize("bad", ["a\tb", "a\x1bb", "a b", "a\x85b", "a\x00b"])
def test_control_characters_are_not_values(bad):
    with pytest.raises(TemplateError, match="control character"):
        check_values(one("echo {{v}}"), {"v": bad})


def test_a_number_is_ascii_digits_only():
    n = template(
        [{"title": "a", "run": "echo {{v}}"}],
        [{"name": "v", "label": "N", "kind": "number"}],
    )
    for bad in ("²", "٢٢", "1.5", "-1"):
        with pytest.raises(TemplateError):
            check_values(n, {"v": bad})
    assert check_values(n, {"v": "22"}) == {"v": "22"}


def test_a_password_that_is_too_short_to_mask_is_refused():
    with pytest.raises(TemplateError, match="at least"):
        check_values(one("echo {{v}}", "password"), {"v": "abc"})


# ---- masking -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("cut", range(31))
def test_a_secret_split_across_two_reads_is_masked(cut):
    secret = "hunter2-secret"
    text = "xxxxxxxxxx" + secret + "yyyyyyyyyy\n"  # 10 + 14 + 10 + 1 = 35
    masker = Masker([secret])

    out = masker.feed(text[:cut]) + masker.feed(text[cut:]) + masker.flush()

    assert out == "xxxxxxxxxx" + MASK + "yyyyyyyyyy\n"


def test_nothing_that_could_start_a_secret_is_released_early():
    masker = Masker(["hunter2-secret"])
    first = masker.feed("log: hunte")
    assert "hunte" not in first  # held back until the next piece decides
    assert first + masker.feed("r2-secret done") + masker.flush() == f"log: {MASK} done"


def test_the_quoted_spelling_of_a_secret_is_masked_too():
    secret = "it's a secret"
    shown = preview(one("mysql -p{{v}}", "password"), {"v": secret})[0][1]
    assert secret not in shown and "'it'\"'\"'s a secret'" not in shown
    assert MASK in shown


def test_a_one_character_secret_cannot_mangle_ordinary_text():
    assert mask("abc 1 and 12", ["1"]) == "abc 1 and 12"


# ---- a file step keeps its content off the command line ------------------------------------------


class Recorder:
    def __init__(self, fail_on=None, raise_on=None):
        self.commands, self.puts = [], []
        self.fail_on, self.raise_on = fail_on, raise_on

    def run(self, command, timeout=60):
        self.commands.append(command)
        return Result(0, "", "")

    def run_stream(self, command, on_text, timeout=0):
        self.commands.append(command)
        if self.raise_on and self.raise_on in command:
            raise SshError("connection lost")
        on_text("output of " + command[:20])
        return 1 if self.fail_on and self.fail_on in command else 0

    def put_bytes(self, path, data, mode="600"):
        self.puts.append((path, data.decode(), mode))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def test_file_content_travels_on_the_channel_not_in_argv():
    t = template(
        [
            {
                "title": "w",
                "kind": "file",
                "path": "/etc/app.env",
                "run": "PASSWORD={{pw}}\n",
                "mode": "600",
            }
        ],
        [{"name": "pw", "label": "P", "kind": "password"}],
    )
    conn = Recorder()

    assert run_template(conn, t, {"pw": "Sup3rSecret-9"}).ok

    assert conn.puts == [("/etc/app.env", "PASSWORD=Sup3rSecret-9\n", "600")]
    joined = " ".join(conn.commands)
    assert "Sup3rSecret" not in joined
    assert "UEFTU1dPUkQ" not in joined  # nor its base64


@pytest.mark.parametrize("mode", ["644; echo EVIL", "9999", "rw-r--r--", ""])
def test_a_file_mode_is_only_octal_digits(mode):
    with pytest.raises(TemplateError, match="mode"):
        template(
            [{"title": "w", "kind": "file", "path": "/x", "run": "x", "mode": mode}]
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"ignore_failure": "false"},
        {"dangerous": "no"},
        {"timeout": "5"},
        {"timeout": 0},
        {"timeout": -3},
        {"timeout": True},
        {"title": 5},
        {"run": 5},
    ],
)
def test_step_fields_have_the_types_they_claim(bad):
    step = {"title": "a", "run": "true", **bad}
    with pytest.raises(TemplateError):
        template([step])


@pytest.mark.parametrize(
    "raw",
    [
        {"id": "x", "name": "X", "variables": 5, "steps": [{"title": "a", "run": "t"}]},
        {"id": "x", "name": "X", "steps": "nope"},
        {
            "id": "x",
            "name": "X",
            "variables": [{"name": 5, "label": "l"}],
            "steps": [{"title": "a", "run": "t"}],
        },
        {
            "id": "x",
            "name": "X",
            "variables": [{"name": "v", "label": "l", "pattern": 5}],
            "steps": [{"title": "a", "run": "t"}],
        },
        {"id": "x\n", "name": "X", "steps": [{"title": "a", "run": "t"}]},
    ],
)
def test_any_shape_of_json_is_a_template_or_a_template_error(raw):
    with pytest.raises(TemplateError):
        parse(raw)


# ---- a run that is cut short -------------------------------------------------------------------------


def test_a_connection_lost_in_the_middle_keeps_what_the_earlier_steps_did():
    t = template(
        [
            {"title": "one", "run": "echo one"},
            {"title": "two", "run": "echo two"},
            {"title": "three", "run": "echo three"},
        ]
    )
    conn = Recorder(raise_on="echo two")

    result = run_template(conn, t, {})

    assert [(o.title, o.code) for o in result.outcomes] == [("one", 0), ("two", -1)]
    assert not result.ok and result.failed_step == "two"
    assert "connection lost" in result.outcomes[1].output


def test_an_ignored_failure_is_marked_as_ignored():
    t = template(
        [
            {"title": "optional", "run": "x", "ignore_failure": True},
            {"title": "after", "run": "y"},
        ]
    )
    result = run_template(Recorder(fail_on="bash -o pipefail -c x"), t, {})
    assert result.ok and result.outcomes[0].skipped and result.outcomes[0].code == 1
    assert result.failed_step is None


def test_a_secret_split_by_the_connection_never_reaches_the_screen():
    t = template(
        [{"title": "a", "run": "cat"}],
        [{"name": "pw", "label": "P", "kind": "password"}],
    )
    seen = []

    class Splitting(Recorder):
        def run_stream(self, command, on_text, timeout=0):
            for piece in ("pass: hunte", "r2-secret\n"):
                on_text(piece)
            return 0

    result = run_template(
        Splitting(), t, {"pw": "hunter2-secret"}, lambda kind, text: seen.append(text)
    )

    assert "hunter2" not in "".join(seen) and "hunter2" not in result.outcomes[0].output
    assert MASK in "".join(seen)


# ---- on several servers ---------------------------------------------------------------------------------


def test_any_error_on_one_server_is_its_own_result_and_is_logged(tmp_path):
    t = template([{"title": "a", "run": "true"}])
    servers = [
        Server(name="a", host="1"),
        Server(name="b", host="2"),
        Server(name="c", host="3"),
    ]

    def factory(server):
        if server.name == "a":
            raise OSError("socket closed")
        if server.name == "b":
            raise AttributeError("'NoneType' object has no attribute 'open_session'")
        return Recorder()

    results = run_on_servers(servers, t, {}, factory, tmp_path / "log.jsonl")

    assert [(r.server, r.ok) for r in results] == [
        ("a", False),
        ("b", False),
        ("c", True),
    ]
    assert "socket closed" in results[0].detail
    assert len(actionlog.read(tmp_path / "log.jsonl")) == 3


# ---- the library ---------------------------------------------------------------------------------------------


def make_json(tid="mine", name="Mine"):
    return json.dumps(
        {"id": tid, "name": name, "steps": [{"title": "a", "run": "true"}]}
    )


def test_a_paste_cannot_overwrite_a_template_you_already_have(tmp_path):
    library = Library(tmp_path)
    library.import_text(make_json())

    with pytest.raises(TemplateError, match="already have"):
        library.import_text(make_json(name="Other"))
    assert library.get("mine").name == "Mine"

    library.import_text(make_json(name="Edited"), replace="mine")  # editing is fine
    assert library.get("mine").name == "Edited"


def test_a_file_whose_name_does_not_match_its_id_is_reported_not_listed(tmp_path):
    (tmp_path / "other-name.json").write_text(make_json("mine"), encoding="utf-8")
    library = Library(tmp_path)

    assert "mine" not in [t.id for t in library.all()]
    assert any("other-name.json" in p and "rename" in p for p in library.problems())


def test_a_file_with_a_builtin_id_is_reported_not_listed(tmp_path):
    builtin_id = builtin_templates()[0].id
    (tmp_path / f"{builtin_id}.json").write_text(
        make_json(builtin_id), encoding="utf-8"
    )
    library = Library(tmp_path)

    assert sum(1 for t in library.all() if t.id == builtin_id) == 1
    assert any("already taken" in p for p in library.problems())


def test_a_file_with_the_wrong_shape_does_not_break_the_library(tmp_path):
    (tmp_path / "bad.json").write_text(
        '{"id": "bad", "name": "B", "variables": 5, "steps": []}'
    )
    library = Library(tmp_path)
    assert len(library.all()) == len(builtin_templates())
    assert any(p.startswith("bad.json") for p in library.problems())


# ---- the built-in templates --------------------------------------------------------------------------------


def bootstrap():
    return next(t for t in builtin_templates() if t.id == "holdfast-bootstrap")


GOOD_KEY = "age1" + "q" * 58


def values(**override):
    return {"host_label": "web-1", "recipient": GOOD_KEY, **override}


@pytest.mark.parametrize(
    "bad", ["age1abc", "age1" + "q" * 57, "age1" + "b" * 58, "age1" + "Q" * 58]
)
def test_an_encryption_key_that_cannot_be_a_real_age_key_is_refused(bad):
    with pytest.raises(TemplateError):
        check_values(bootstrap(), values(recipient=bad))


def run_step_locally(title_part, filled, replacements=None):
    step = next(s for s in bootstrap().steps if title_part in s.title)
    line = render_step(step, filled)
    for old, new in (replacements or {}).items():
        line = line.replace(old, new)
    return subprocess.run(
        [find_bash(), "-c", line], capture_output=True, text=True, check=False
    )


@needs_bash
def test_auto_without_docker_is_stopped_before_anything_is_installed():
    filled = check_values(bootstrap(), values(install_docker="no", backup_mode="auto"))
    done = run_step_locally("сочетание", filled)
    assert done.returncode == 1 and "Docker" in done.stdout

    ok = check_values(bootstrap(), values(install_docker="no", backup_mode="manual"))
    assert run_step_locally("сочетание", ok).returncode == 0
    ok = check_values(bootstrap(), values(install_docker="yes", backup_mode="auto"))
    assert run_step_locally("сочетание", ok).returncode == 0


@needs_bash
def test_a_rerun_does_not_wipe_a_key_that_was_added_by_hand(tmp_path):
    config = tmp_path / "holdfast.toml"
    original = '[encryption]\nrecipients = ["age1sparekeyadded"]\n'
    config.write_text(original)
    filled = check_values(bootstrap(), values())

    done = run_step_locally(
        "ключ шифрования", filled, {"/etc/holdfast/holdfast.toml": config.as_posix()}
    )

    assert done.returncode == 1 and "вручную" in done.stdout
    assert config.read_text() == original


@needs_bash
def test_the_key_is_accepted_when_it_is_already_there(tmp_path):
    config = tmp_path / "holdfast.toml"
    config.write_text(f'[encryption]\nrecipients = ["{GOOD_KEY}"]\n')
    done = run_step_locally(
        "ключ шифрования",
        check_values(bootstrap(), values()),
        {"/etc/holdfast/holdfast.toml": config.as_posix()},
    )
    assert done.returncode == 0


def test_the_final_dry_run_is_not_allowed_to_fail_quietly():
    last = bootstrap().steps[-1]
    assert "backup --dry-run" in last.run and not last.ignore_failure


def test_the_bootstrap_installs_cron_and_survives_apt_prompts():
    text = " ".join(s.run for s in bootstrap().steps)
    assert " cron" in text
    assert "--force-confold" in text and "DPkg::Lock::Timeout" in text
