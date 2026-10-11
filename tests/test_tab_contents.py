import tomllib
from pathlib import Path

import pytest
from ui_support import FakeConn, application, enrolled, make_ctx, settle

from holdfast_panel import actionlog
from holdfast_panel.ssh import Result
from holdfast_panel.ui.contents_tab import ContentsTab

REAL = (Path(__file__).parent / "fixtures" / "holdfast_init.toml").read_text("utf-8")


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


class Server(FakeConn):
    """A server holding holdfast.toml as a file the commands really change."""

    def __init__(self, text, dry_run_codes=(0, 0)):
        super().__init__()
        self.files = {"/etc/holdfast/holdfast.toml": text}
        self.dry = list(dry_run_codes)

    def run(self, command, timeout=60):
        self.log.append(command)
        if command.startswith("cat /etc/holdfast/holdfast.toml"):
            return Result(0, self.files["/etc/holdfast/holdfast.toml"], "")
        if command == "holdfast backup --dry-run":
            code = self.dry.pop(0) if self.dry else 0
            return Result(
                code,
                "mode: manual\nplan line\n" if code == 0 else "",
                "boom" if code else "",
            )
        if command.startswith("date"):
            return Result(0, "20261011-120000\n", "")
        for verb in ("cp -p ", "mv -f "):
            if command.startswith(verb):
                src, dst = command[len(verb) :].split()
                self.files[dst] = self.files[src]
                if verb == "mv -f ":
                    del self.files[src]
        return Result(0, "", "")

    def put_bytes(self, path, data, mode="600"):
        self.files[path] = data.decode()


def build(tmp_path, server):
    ctx = make_ctx(tmp_path, conn_factory=lambda s: server, servers=[enrolled()])
    tab = ContentsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    settle(ctx)
    return ctx, tab


def test_the_file_and_its_form_are_loaded(tmp_path):
    server = Server(REAL)
    _ctx, tab = build(tmp_path, server)

    assert tab.editor.toPlainText() == REAL
    assert tab.mode.currentText() == "auto"
    assert tab.excludes.count() == 0
    assert tab.message.text() == "Файл загружен."


def test_the_form_edits_the_text_and_only_the_two_keys(tmp_path):
    _ctx, tab = build(tmp_path, Server(REAL))
    tab.mode.setCurrentText("manual")
    tab.new_exclude.setText("volume:cache")
    tab.add_exclude()
    tab.new_exclude.setText("path:/srv/old")
    tab.add_exclude()
    tab.apply_form()

    parsed = tomllib.loads(tab.editor.toPlainText())
    assert parsed["backup"] == {
        "mode": "manual",
        "exclude": ["volume:cache", "path:/srv/old"],
    }
    assert parsed["api"] == {"token": "REDACTED"}  # untouched
    assert tab.editor.toPlainText().startswith("# holdfast configuration")


def test_a_bad_exclusion_is_refused_in_the_form(tmp_path):
    _ctx, tab = build(tmp_path, Server(REAL))
    tab.new_exclude.setText("nonsense")
    tab.add_exclude()
    assert tab.excludes.count() == 0 and "volume:" in tab.message.text()


def test_removing_an_exclusion_from_the_form(tmp_path):
    _ctx, tab = build(tmp_path, Server(REAL))
    for item in ("volume:a", "volume:b"):
        tab.new_exclude.setText(item)
        tab.add_exclude()
    tab.excludes.setCurrentRow(0)
    tab.remove_exclude()
    assert tab.form_excludes() == ["volume:b"]


def test_saving_asks_with_the_diff_then_writes_and_keeps_a_copy(tmp_path):
    server = Server(REAL)
    ctx, tab = build(tmp_path, server)
    tab.mode.setCurrentText("manual")
    tab.apply_form()

    tab.save()
    settle(ctx)

    _title, question = ctx.confirmed[-1]
    assert '-mode = "auto"' in question and '+mode = "manual"' in question
    assert (
        tomllib.loads(server.files["/etc/holdfast/holdfast.toml"])["backup"]["mode"]
        == "manual"
    )
    assert server.files["/etc/holdfast/holdfast.toml.bak-20261011-120000"] == REAL
    assert actionlog.read(ctx.log_path)[0].ok
    assert "сохранено" in tab.message.text()
    # saved: the editor is now the baseline, so saving again changes nothing
    tab.save()
    assert tab.message.text() == "Изменений нет."


def test_an_edit_that_breaks_the_backup_is_rolled_back_and_reported(tmp_path):
    server = Server(REAL, dry_run_codes=(0, 1))
    ctx, tab = build(tmp_path, server)
    tab.mode.setCurrentText("manual")
    tab.apply_form()

    tab.save()
    settle(ctx)

    assert server.files["/etc/holdfast/holdfast.toml"] == REAL
    assert "возвращён" in tab.message.text()
    assert not actionlog.read(ctx.log_path)[0].ok


def test_declining_the_diff_changes_nothing(tmp_path):
    server = Server(REAL)
    ctx, tab = build(tmp_path, server)
    ctx.confirm = lambda title, text: False
    tab.mode.setCurrentText("manual")
    tab.apply_form()

    tab.save()
    settle(ctx)

    assert server.files == {"/etc/holdfast/holdfast.toml": REAL}


def test_invalid_toml_is_not_sent(tmp_path):
    server = Server(REAL)
    ctx, tab = build(tmp_path, server)
    tab.editor.setPlainText('mode = "manual')

    tab.save()
    settle(ctx)

    assert "Не сохранено" in tab.message.text() and not ctx.confirmed
    assert server.files == {"/etc/holdfast/holdfast.toml": REAL}


def test_the_plan_button_shows_what_will_be_taken(tmp_path):
    ctx, tab = build(tmp_path, Server(REAL))
    tab.show_plan()
    settle(ctx)
    assert "plan line" in tab.log_view.toPlainText()


def test_a_server_without_a_key_has_nothing_to_edit(tmp_path):
    from holdfast_panel.store import Server as Record

    ctx = make_ctx(tmp_path, servers=[Record(name="new", host="1")])
    tab = ContentsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    assert not tab.save_button.isEnabled() and tab.editor.toPlainText() == ""
