"""Small findings of the reviews, each with a test that fails without the fix."""

import os

import pytest
from ui_support import application, make_ctx, settle

from holdfast_panel.builtin_templates import builtin_templates
from holdfast_panel.store import Server, ServerStore
from holdfast_panel.ui import main_window
from holdfast_panel.ui.add_dialog import AddServerDialog
from holdfast_panel.ui.main_window import MainWindow


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


# ---- writing the list of servers ----------------------------------------------------------


def test_a_replace_that_is_blocked_for_a_moment_is_retried(tmp_path, monkeypatch):
    store = ServerStore(tmp_path / "servers.json")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("held open by a scanner")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    store.add(Server(name="a", host="1"))

    assert len(calls) == 3 and [s.name for s in store.load()] == ["a"]


def test_a_replace_that_never_succeeds_fails_loudly_and_leaves_no_temp_file(
    tmp_path, monkeypatch
):
    store = ServerStore(tmp_path / "servers.json")

    def blocked(src, dst):
        raise PermissionError("always held")

    monkeypatch.setattr(os, "replace", blocked)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(PermissionError):
        store.add(Server(name="a", host="1"))

    assert [p.name for p in tmp_path.iterdir()] == []  # no stray .tmp


def test_two_writers_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    store = ServerStore(tmp_path / "servers.json")
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: seen.append(s) or real(s, d))

    store.save([Server(name="a", host="1")])
    store.save([Server(name="a", host="1")])

    assert len(set(seen)) == 2  # a fixed ".tmp" name would be one and the same


# ---- a server whose key never got installed -----------------------------------------------------


def test_adding_again_a_server_that_was_never_enrolled_updates_it(
    tmp_path, monkeypatch
):
    ctx = make_ctx(tmp_path, servers=[Server(name="web-1", host="old.example")])
    before = ctx.servers.load()[0]

    def fake_exec(dialog):
        dialog.name.setText("web-1")
        dialog.host.setText("new.example")
        dialog.password.setText("pw")
        return AddServerDialog.DialogCode.Accepted

    monkeypatch.setattr(AddServerDialog, "exec", fake_exec)
    monkeypatch.setattr(
        main_window, "fetch_host_fingerprint", lambda h, p: "SHA256:fresh"
    )
    monkeypatch.setattr(main_window, "enroll", lambda server, password, key: None)
    window = MainWindow(ctx)
    ctx.confirm = lambda title, text: True
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *a, **k: main_window.QMessageBox.StandardButton.Yes,
    )

    window.add_server()
    settle(ctx)

    after = ctx.servers.load()
    assert len(after) == 1 and ctx.errors == []
    assert after[0].id == before.id  # the same record, not a second one
    assert (
        after[0].host == "new.example" and after[0].host_fingerprint == "SHA256:fresh"
    )
    assert after[0].enrolled  # and this time the key was installed


def test_an_enrolled_server_is_still_refused_under_the_same_name(tmp_path, monkeypatch):
    done = Server(name="web-1", host="a", enrolled=True, host_fingerprint="SHA256:x")
    ctx = make_ctx(tmp_path, servers=[done])

    def fake_exec(dialog):
        dialog.name.setText("web-1")
        dialog.host.setText("b")
        dialog.password.setText("pw")
        return AddServerDialog.DialogCode.Accepted

    monkeypatch.setattr(AddServerDialog, "exec", fake_exec)
    monkeypatch.setattr(main_window, "fetch_host_fingerprint", lambda h, p: "SHA256:y")
    monkeypatch.setattr(
        main_window.QMessageBox,
        "question",
        lambda *a, **k: main_window.QMessageBox.StandardButton.Yes,
    )
    window = MainWindow(ctx)

    window.add_server()
    settle(ctx)

    assert [s.host for s in ctx.servers.load()] == ["a"]
    assert any("already exists" in e for e in ctx.errors)


# ---- what the templates say about themselves ----------------------------------------------------


def test_templates_that_publish_ports_or_keep_old_passwords_say_so():
    by_id = {t.id: t for t in builtin_templates()}
    assert "ufw" in by_id["docker-wordpress"].description
    assert "пароль уже созданной базы" in by_id["docker-wordpress"].description
    assert "пароль уже созданной базы" in by_id["docker-postgres"].description
    assert "ufw" in by_id["docker-portainer"].description
