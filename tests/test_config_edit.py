import tomllib
from pathlib import Path

import pytest

from holdfast_panel.config_edit import (
    ConfigEditError,
    diff,
    problems,
    save_config,
    set_backup_mode,
    set_excludes,
    set_section_key,
)
from holdfast_panel.ssh import Result

BASE = """# my machine
host_label = "web-1"

[encryption]
recipients = ["age1abc"]   # primary

[backup]
mode = "auto"
root = "/opt/backups"
exclude = [
  "volume:old",   # a note
  "path:/srv/old",
]
retention_days = 7

[[component]]
type = "path"
name = "letsencrypt"
path = "/etc/letsencrypt"
"""


def test_changing_the_mode_touches_only_that_line():
    new = set_backup_mode(BASE, "manual")
    assert new == BASE.replace('mode = "auto"', 'mode = "manual"')


def test_a_multiline_exclude_is_replaced_whole_and_the_neighbours_survive():
    new = set_excludes(BASE, ["volume:cache", "container:adminer"])
    parsed = tomllib.loads(new)
    assert parsed["backup"]["exclude"] == ["volume:cache", "container:adminer"]
    assert parsed["backup"]["retention_days"] == 7
    assert parsed["backup"]["root"] == "/opt/backups"
    assert parsed["component"][0]["name"] == "letsencrypt"
    assert parsed["encryption"]["recipients"] == ["age1abc"]
    assert "# my machine" in new and "# primary" in new


def test_an_exclude_can_be_emptied():
    assert tomllib.loads(set_excludes(BASE, []))["backup"]["exclude"] == []


def test_a_missing_key_is_added_under_its_section():
    text = '[backup]\nmode = "auto"\n'
    new = set_excludes(text, ["volume:x"])
    assert tomllib.loads(new)["backup"] == {"mode": "auto", "exclude": ["volume:x"]}


def test_a_missing_section_is_appended():
    new = set_backup_mode('host_label = "web-1"\n', "manual")
    assert tomllib.loads(new) == {"host_label": "web-1", "backup": {"mode": "manual"}}
    assert tomllib.loads(set_backup_mode("", "auto")) == {"backup": {"mode": "auto"}}


def test_a_key_with_the_same_name_in_another_section_is_left_alone():
    text = '[other]\nmode = "keep"\n\n[backup]\nmode = "auto"\n'
    parsed = tomllib.loads(set_backup_mode(text, "manual"))
    assert parsed["other"]["mode"] == "keep" and parsed["backup"]["mode"] == "manual"


def test_a_value_with_quotes_and_unicode_survives():
    new = set_section_key("[backup]\n", "backup", "exclude", ['path:/srv/"x" ü'])
    assert tomllib.loads(new)["backup"]["exclude"] == ['path:/srv/"x" ü']


@pytest.mark.parametrize(
    "item",
    ["nonsense", "volume:", "path:relative/dir", "database:justone", "pathe:/x"],
)
def test_a_malformed_exclude_is_refused(item):
    with pytest.raises(ConfigEditError):
        set_excludes(BASE, [item])


def test_a_bad_mode_is_refused():
    with pytest.raises(ConfigEditError):
        set_backup_mode(BASE, "sometimes")


def test_problems_found_without_a_server():
    assert problems(BASE) == []
    assert problems("= nope")[0].startswith("not valid TOML")
    text = '[backup]\nmode = "x"\nexclude = ["bad"]\n[[component]]\nname = "a"\n'
    found = problems(text + '[encryption]\nrecipients = ["secret"]\n')
    assert len(found) == 4
    assert any("backup.mode" in f for f in found)
    assert any("component" in f for f in found)
    assert any("age public key" in f for f in found)


def test_the_diff_shows_only_what_changed():
    text = diff(BASE, set_backup_mode(BASE, "manual"))
    changed = [
        line
        for line in text.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed == ['-mode = "auto"', '+mode = "manual"']


def test_edits_work_on_the_file_holdfast_init_really_writes():
    real = (Path(__file__).parent / "fixtures" / "holdfast_init.toml").read_text(
        "utf-8"
    )
    assert problems(real) == []

    new = set_excludes(set_backup_mode(real, "manual"), ["volume:cache"])
    parsed = tomllib.loads(new)

    assert parsed["backup"] == {"mode": "manual", "exclude": ["volume:cache"]}
    # nothing else moved: the secret, the label and the comments are all there
    assert parsed["api"] == {"token": "REDACTED"}
    assert parsed["host_label"] == "web-1" and parsed["encryption"]["recipients"] == []
    assert new.startswith("# holdfast configuration for this machine.")


# ---- saving ------------------------------------------------------------------


class Server:
    def __init__(self, dry_runs):
        self.dry_runs = list(dry_runs)  # exit codes, in order
        self.files = {"/etc/holdfast/holdfast.toml": BASE}
        self.log = []

    def run(self, command, timeout=60):
        self.log.append(command)
        if command == "holdfast backup --dry-run":
            code = self.dry_runs.pop(0)
            return Result(
                code, "plan" if code == 0 else "", "" if code == 0 else "boom"
            )
        if command.startswith("date"):
            return Result(0, "20261011-120000\n", "")
        if command.startswith("cp -p "):
            src, dst = command[6:].split()
            self.files[dst] = self.files[src]
        if command.startswith("mv -f "):
            src, dst = command[6:].split()
            self.files[dst] = self.files.pop(src)
        return Result(0, "", "")

    def put_bytes(self, path, data, mode="600"):
        self.files[path] = data.decode()


def test_a_saved_file_replaces_the_old_one_and_keeps_a_copy():
    server = Server([0, 0])
    new = set_backup_mode(BASE, "manual")

    result = save_config(server, new)

    assert not result.rolled_back and result.dry_run == "plan"
    assert server.files["/etc/holdfast/holdfast.toml"] == new
    assert server.files[result.backup_path] == BASE
    assert result.backup_path.endswith(".bak-20261011-120000")
    assert "/etc/holdfast/holdfast.toml.new" not in server.files


def test_an_edit_that_breaks_the_backup_is_undone():
    server = Server([0, 1])
    result = save_config(server, set_backup_mode(BASE, "manual"))

    assert result.rolled_back and "boom" in result.dry_run
    assert server.files["/etc/holdfast/holdfast.toml"] == BASE


def test_a_server_whose_dry_run_was_already_failing_is_not_rolled_back():
    server = Server([1, 1])
    new = set_backup_mode(BASE, "manual")
    result = save_config(server, new)

    assert not result.rolled_back
    assert server.files["/etc/holdfast/holdfast.toml"] == new


def test_an_invalid_file_is_never_sent():
    server = Server([0])
    with pytest.raises(ConfigEditError):
        save_config(server, 'mode = "manual\n')
    assert server.log == []
