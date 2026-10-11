import json

import pytest
from ui_support import FakeConn, application, enrolled, make_ctx, settle

from holdfast_panel import actionlog
from holdfast_panel.ssh import Result, SshError
from holdfast_panel.ui.templates_tab import SKELETON, TemplateEditor, TemplatesTab

RECIPIENT = "age1" + "q" * 58


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


def build(tmp_path, conn_factory=None, servers=None):
    ctx = make_ctx(
        tmp_path,
        conn_factory=conn_factory,
        servers=servers if servers is not None else [enrolled()],
    )
    tab = TemplatesTab(ctx)
    tabs_server = ctx.servers.load()
    tab.server = tabs_server[0] if tabs_server else None
    tab.refresh()
    return ctx, tab


def select(tab, template_id):
    index = next(i for i, t in enumerate(tab.templates) if t.id == template_id)
    tab.listing.setCurrentRow(index)


def edit_in_dialog(monkeypatch, text):
    """Stand in for the modal editor: type `text`, press Save."""

    def fake_exec(dialog):
        dialog.editor.setPlainText(text)
        dialog.save()
        return TemplateEditor.DialogCode.Accepted if dialog.saved else 0

    monkeypatch.setattr(TemplateEditor, "exec", fake_exec)


# ---- the list and the form ----------------------------------------------------


def test_builtin_templates_are_listed_and_the_form_follows_the_selection(tmp_path):
    _ctx, tab = build(tmp_path)

    names = [tab.listing.item(i).text() for i in range(tab.listing.count())]
    assert any("Первичная установка" in n for n in names)
    select(tab, "holdfast-bootstrap")
    assert set(tab.fields) == {
        "host_label",
        "recipient",
        "backup_mode",
        "install_docker",
        "cron_hour",
        "repo",
    }
    assert tab.fields["backup_mode"].text() == "auto"  # the default is filled in
    select(tab, "docker-wordpress")
    assert tab.fields["db_password"].echoMode().name == "Password"
    select(tab, "firewall-ufw")
    assert "опасные шаги" in tab.description.text()


def test_the_preview_shows_commands_and_hides_passwords(tmp_path):
    _ctx, tab = build(tmp_path)
    select(tab, "docker-wordpress")
    tab.fields["project"].setText("myapp")
    tab.fields["db_password"].setText("Sup3rSecret-db")
    tab.fields["root_password"].setText("Sup3rSecret-root")

    tab.show_preview()

    shown = tab.log_view.toPlainText()
    assert "docker compose -p myapp up -d" in shown
    assert "Sup3rSecret" not in shown and "••••••" in shown


def test_a_bad_value_is_reported_and_nothing_runs(tmp_path):
    asked = []
    ctx, tab = build(tmp_path, lambda s: asked.append(s) or FakeConn())
    select(tab, "holdfast-bootstrap")
    tab.fields["host_label"].setText("web 1; reboot")
    tab.fields["recipient"].setText("age1abc'; reboot; '")

    tab.run_here()
    settle(ctx)

    assert "host_label" in tab.message.text() or "Имя машины" in tab.message.text()
    assert asked == [] and ctx.confirmed == []


# ---- running -----------------------------------------------------------------


def test_a_template_runs_step_by_step_on_the_selected_server(tmp_path):
    conn = FakeConn()
    ctx, tab = build(tmp_path, lambda s: conn)
    select(tab, "system-update")

    tab.run_here()
    settle(ctx)

    _title, question = ctx.confirmed[-1]
    assert "apt-get update" in question  # what will run was shown first
    assert [c for c in conn.log if c.startswith("bash -o pipefail -c")]
    assert "▶ Обновить пакеты" in tab.log_view.toPlainText()
    entry = actionlog.read(ctx.log_path)[0]
    assert entry.action == "шаблон system-update" and entry.ok


def test_the_bootstrap_runs_with_valid_values_and_every_step_in_order(tmp_path):
    conn = FakeConn()
    ctx, tab = build(tmp_path, lambda s: conn)
    select(tab, "holdfast-bootstrap")
    tab.fields["host_label"].setText("web-1")
    tab.fields["recipient"].setText(RECIPIENT)

    tab.run_here()
    settle(ctx)

    joined = "\n".join(conn.log)
    for needle in (
        "apt-get update",
        "get.docker.com",
        "git clone",
        "pip install",
        "holdfast init --fresh --host-label web-1",
        RECIPIENT,
        "holdfast config check",
    ):
        assert needle in joined, needle
    assert joined.index("apt-get update") < joined.index("holdfast config check")
    assert actionlog.read(ctx.log_path)[0].ok


def test_a_failing_step_stops_the_run_and_is_logged_as_failed(tmp_path):
    conn = FakeConn({"apt-get install": Result(100, "E: unable to locate\n", "")})
    ctx, tab = build(tmp_path, lambda s: conn)
    select(tab, "fail2ban")

    tab.run_here()
    settle(ctx)

    assert "systemctl enable" not in "\n".join(conn.log)
    entry = actionlog.read(ctx.log_path)[0]
    assert not entry.ok and "Установить fail2ban" in entry.detail
    assert "unable to locate" in tab.log_view.toPlainText()


def test_a_dangerous_template_shows_its_warning_in_the_question(tmp_path):
    ctx, tab = build(tmp_path)
    select(tab, "firewall-ufw")
    tab.fields["ssh_port"].setText("2222")

    tab.run_here()
    settle(ctx)

    assert (
        "⚠ ОПАСНО" in ctx.confirmed[-1][1]
        and "ufw allow 2222/tcp" in ctx.confirmed[-1][1]
    )


def test_declining_runs_nothing(tmp_path):
    conn = FakeConn()
    ctx, tab = build(tmp_path, lambda s: conn)
    ctx.confirm = lambda title, text: False
    select(tab, "system-update")

    tab.run_here()
    settle(ctx)

    assert conn.log == []


def test_a_password_never_appears_in_the_output_or_the_log(tmp_path):
    secret = "Sup3rSecret-db1"
    conn = FakeConn({"base64 -d": Result(0, f"wrote file with {secret}\n", "")})
    ctx, tab = build(tmp_path, lambda s: conn)
    select(tab, "docker-wordpress")
    tab.fields["project"].setText("myapp")
    tab.fields["db_password"].setText(secret)
    tab.fields["root_password"].setText("Sup3rSecret-root")

    tab.run_here()
    settle(ctx)

    assert secret not in tab.log_view.toPlainText()
    assert secret not in ctx.log_path.read_text("utf-8")
    assert "••••••" in tab.log_view.toPlainText()


# ---- on every server -----------------------------------------------------------


def test_one_server_failing_does_not_stop_the_rest_and_each_is_logged_once(tmp_path):
    servers = [enrolled("a", "1"), enrolled("down", "2"), enrolled("c", "3")]

    def factory(server):
        if server.name == "down":
            raise SshError("timed out")
        return FakeConn()

    ctx, tab = build(tmp_path, factory, servers=servers)
    select(tab, "system-update")

    tab.run_everywhere()
    settle(ctx)

    assert "a, down, c" in ctx.confirmed[-1][1]
    entries = actionlog.read(ctx.log_path)
    assert sorted(e.server for e in entries) == ["a", "c", "down"]
    assert {e.server: e.ok for e in entries} == {"a": True, "down": False, "c": True}
    assert "сбой на: down" in tab.stage_label.text()
    assert "######## c ########" in tab.log_view.toPlainText()


def test_running_everywhere_needs_a_server_with_a_key(tmp_path):
    from holdfast_panel.store import Server

    ctx, tab = build(tmp_path, servers=[Server(name="new", host="1")])
    select(tab, "system-update")
    tab.run_everywhere()
    assert "Нет серверов" in tab.message.text() and ctx.confirmed == []


# ---- the library -----------------------------------------------------------------


def test_a_new_template_is_edited_saved_and_run(tmp_path, monkeypatch):
    conn = FakeConn()
    ctx, tab = build(tmp_path, lambda s: conn)
    edit_in_dialog(monkeypatch, json.dumps(SKELETON))

    tab.new_template()

    assert tab.current().id == "my-template" and tab.fields["name"].text() == "world"
    tab.fields["name"].setText("панель")
    tab.run_here()
    settle(ctx)
    assert any("hello" in c and "панель" in c for c in conn.log)
    assert (tmp_path / "templates" / "my-template.json").is_file()


def test_a_template_that_does_not_parse_is_not_saved(tmp_path):
    ctx, _tab = build(tmp_path)
    dialog = TemplateEditor(ctx, '{"id": "x", "name": "X", "steps": []}')
    dialog.save()

    assert dialog.saved is None and "Не сохранено" in dialog.message.text()
    assert not (tmp_path / "templates").exists()


def test_a_builtin_is_not_edited_or_deleted_but_can_be_copied(tmp_path, monkeypatch):
    _ctx, tab = build(tmp_path)
    select(tab, "fail2ban")

    tab.edit_template()
    assert "копию" in tab.message.text()
    tab.delete_template()
    assert "нельзя" in tab.message.text()

    tab.copy_template()
    assert tab.current().id == "fail2ban-copy" and not tab.current().builtin
    assert tab.current().name.endswith("(копия)")
    select(tab, "fail2ban")
    tab.copy_template()  # a second copy gets its own id
    assert tab.current().id == "fail2ban-copy2"


def test_a_users_template_can_be_edited_and_deleted(tmp_path, monkeypatch):
    ctx, tab = build(tmp_path)
    select(tab, "fail2ban")
    tab.copy_template()

    edited = tab.current().to_dict()
    edited["name"] = "Другое имя"
    edit_in_dialog(monkeypatch, json.dumps(edited))
    tab.edit_template()
    assert tab.current().name == "Другое имя"

    tab.delete_template()
    assert not any(t.id == "fail2ban-copy" for t in tab.templates)
    assert ctx.confirmed[-1][0] == "Удалить шаблон"


def test_pasted_json_is_imported(tmp_path, monkeypatch):
    _ctx, tab = build(tmp_path)
    pasted = {
        "id": "from-chat",
        "name": "Из чата",
        "steps": [{"title": "a", "run": "true"}],
    }
    edit_in_dialog(monkeypatch, json.dumps(pasted))

    tab.import_template()

    assert tab.current().id == "from-chat"
