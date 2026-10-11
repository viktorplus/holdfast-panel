import json
from pathlib import Path

import pytest
from ui_support import FakeConn, application, enrolled, make_ctx, settle

from holdfast_panel import actionlog
from holdfast_panel.ssh import Result
from holdfast_panel.store import Storage
from holdfast_panel.ui.backups_tab import SERVER, BackupsTab
from holdfast_panel.ui.storages_dialog import StoragesDialog

FIXTURES = Path(__file__).parent / "fixtures" / "pc"
MANIFEST = (FIXTURES / "web-1" / "20260920-231500" / "manifest.json").read_text("utf-8")


def sections(*names):
    return "".join(f"##{n}\n{MANIFEST}\n" for n in names)


def answers(**override):
    base = {
        "cat /etc/holdfast/holdfast.toml": Result(0, 'host_label = "web-1"\n', ""),
        "for d in /opt/backups": Result(
            0, sections("20260921-020000", "20260920-231500"), ""
        ),
        "rclone lsf": Result(0, "20260919-020000/\n", ""),
        "rclone cat": Result(0, sections("20260919-020000"), ""),
        "test -e": Result(1, "", ""),  # not on the server yet
        "holdfast backup --dry-run": Result(
            0, "mode: auto\nproject-myapp (path)\n", ""
        ),
        "holdfast backup": Result(
            0, "/opt/backups/20261011-020000 (10 bytes in 3 artifacts)\n", ""
        ),
        "holdfast restore verify": Result(0, "checked 3 artifacts\n", ""),
        "--dry-run": Result(
            0, "This will OVERWRITE live data on web-1.\n  path project-myapp\n", ""
        ),
        "--yes": Result(0, "restored 3 artifacts\n", ""),
    }
    base.update(override)
    return base


@pytest.fixture(scope="module", autouse=True)
def app():
    return application()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "key.txt"
    path.write_text("AGE-SECRET-KEY-1TEST\n")
    return path


def build(tmp_path, conn=None, key=None, **override):
    conn = conn or FakeConn(answers(**override))
    ctx = make_ctx(tmp_path, conn_factory=lambda server: conn, servers=[enrolled()])
    ctx.ask_key_file = lambda: key
    tab = BackupsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()
    settle(ctx)
    return ctx, tab, conn


def commands(conn):
    return conn.log


# ---- looking ---------------------------------------------------------------


def test_the_server_snapshots_are_listed_newest_first(tmp_path):
    ctx, tab, _ = build(tmp_path)

    assert tab.table.rowCount() == 2
    assert tab.table.item(0, 1).text() == "web-1"
    assert "3:" in tab.table.item(0, 3).text()
    assert "Снимков: 2" in tab.note.text() and "web-1" in tab.note.text()
    assert tab.table.item(0, 4).text() == "нет"  # the fixture is not encrypted
    assert ctx.errors == []


def test_a_folder_on_this_computer_is_a_place_to_look(tmp_path):
    ctx, tab, _ = build(tmp_path)
    ctx.storages.add(Storage(name="Ноутбук", kind="pc", location=str(FIXTURES)))
    tab.refresh()
    tab.where.setCurrentIndex(1)
    settle(ctx)

    assert tab.current_place() == ("pc", str(FIXTURES))
    assert tab.table.rowCount() == 1
    assert "Этот компьютер: Ноутбук" in tab.where.itemText(1)


def test_a_remote_is_listed_through_the_server(tmp_path):
    ctx, tab, conn = build(tmp_path)
    ctx.storages.add(
        Storage(name="Хранилище 1", kind="remote", location="shared:backups")
    )
    tab.refresh()
    tab.where.setCurrentIndex(1)
    settle(ctx)

    assert tab.table.rowCount() == 1
    assert any(
        "rclone lsf --dirs-only shared:backups/web-1/" in c for c in commands(conn)
    )


def test_a_server_without_a_key_shows_no_list_and_disables_the_buttons(tmp_path):
    from holdfast_panel.store import Server

    ctx = make_ctx(tmp_path, servers=[Server(name="new", host="1")])
    tab = BackupsTab(ctx)
    tab.server = ctx.servers.load()[0]
    tab.refresh()

    assert tab.table.rowCount() == 0 and "Ключ ещё не установлен" in tab.note.text()
    assert not tab.create_button.isEnabled()


# ---- making a backup ---------------------------------------------------------


def test_a_backup_shows_the_plan_runs_and_is_written_to_the_log(tmp_path):
    ctx, tab, conn = build(tmp_path)

    tab.create_backup()
    settle(ctx)

    out = tab.log_view.toPlainText()
    assert "project-myapp (path)" in out  # the plan from --dry-run
    assert "bytes in 3 artifacts" in out
    runs = [c for c in commands(conn) if c.startswith("holdfast backup")]
    assert runs == ["holdfast backup --dry-run", "holdfast backup"]
    entry = actionlog.read(ctx.log_path)[0]
    assert entry.server == "web-1" and entry.action == "бэкап" and entry.ok
    assert tab.create_button.isEnabled()


def test_a_backup_that_fails_is_logged_as_failed(tmp_path):
    ctx, tab, _ = build(tmp_path, **{"holdfast backup": Result(1, "no space\n", "")})
    # the dry-run key still matches first, so only the real run fails
    tab.create_backup()
    settle(ctx)

    entry = actionlog.read(ctx.log_path)[0]
    assert not entry.ok
    assert "не удалось" in tab.stage_label.text().lower()


def test_declining_the_backup_confirmation_runs_nothing(tmp_path):
    ctx, tab, conn = build(tmp_path)
    ctx.confirm = lambda title, text: False
    before = len(conn.log)

    tab.create_backup()
    settle(ctx)

    assert len(conn.log) == before


# ---- restoring ---------------------------------------------------------------


def test_a_restore_verifies_plans_asks_and_removes_the_key(tmp_path, key_file):
    ctx, tab, conn = build(tmp_path, key=key_file)
    tab.table.selectRow(0)

    tab.restore()
    settle(ctx)

    seen = commands(conn)
    verify = next(i for i, c in enumerate(seen) if "restore verify" in c)
    plan = next(
        i for i, c in enumerate(seen) if c.endswith("--dry-run") and "restore" in c
    )
    real = next(i for i, c in enumerate(seen) if c.endswith("--yes"))
    assert verify < plan < real
    assert any(c.startswith("put /root/.holdfast-restore-key-") for c in seen)
    assert any(c.startswith("shred -u /root/.holdfast-restore-key-") for c in seen)
    # the plan reached the user, on the GUI thread, before anything was written
    assert any("OVERWRITE" in text for _, text in ctx.confirmed)
    assert actionlog.read(ctx.log_path)[0].ok
    assert ctx.errors == []


def test_declining_the_plan_writes_nothing_and_removes_the_key(tmp_path, key_file):
    ctx, tab, conn = build(tmp_path, key=key_file)
    ctx.confirm = lambda title, text: False
    tab.table.selectRow(0)

    tab.restore()
    settle(ctx)

    assert not any(c.endswith("--yes") for c in commands(conn))
    assert any(c.startswith("shred -u") for c in commands(conn))
    assert not actionlog.read(ctx.log_path)[0].ok


def test_a_restore_without_a_key_file_does_not_touch_the_server(tmp_path):
    ctx, tab, conn = build(tmp_path, key=None)
    tab.table.selectRow(0)
    before = len(conn.log)

    tab.restore()
    settle(ctx)

    assert len(conn.log) == before
    assert any("ключ" in e for e in ctx.errors)


def test_a_restore_without_a_selected_snapshot_asks_for_one(tmp_path, key_file):
    ctx, tab, _conn = build(tmp_path, key=key_file)
    tab.table.clearSelection()
    tab.table.setCurrentCell(-1, -1)

    tab.restore()

    assert any("Выберите снимок" in e for e in ctx.errors)


def test_a_snapshot_from_another_machine_is_restored_with_the_flag(tmp_path, key_file):
    manifest = json.loads(MANIFEST)
    manifest["host_label"] = "web-OLD"
    other = f"##20260921-020000\n{json.dumps(manifest)}\n"
    ctx, tab, conn = build(
        tmp_path, key=key_file, **{"for d in /opt/backups": Result(0, other, "")}
    )
    tab.table.selectRow(0)

    tab.restore()
    settle(ctx)

    assert any(c.endswith("--from-other-host --yes") for c in commands(conn))


def test_a_snapshot_on_this_computer_is_copied_to_the_server_first(tmp_path, key_file):
    ctx, tab, conn = build(tmp_path, key=key_file)
    ctx.storages.add(Storage(name="Ноутбук", kind="pc", location=str(FIXTURES)))
    tab.refresh()
    tab.where.setCurrentIndex(1)
    settle(ctx)
    tab.table.selectRow(0)

    tab.restore()
    settle(ctx)

    seen = commands(conn)
    upload = next(
        i for i, c in enumerate(seen) if c.startswith("upload 20260920-231500")
    )
    first_restore = next(i for i, c in enumerate(seen) if "restore verify" in c)
    assert upload < first_restore
    assert any("--snapshot /opt/backups/20260920-231500" in c for c in seen)


def test_only_a_server_snapshot_can_be_verified(tmp_path, key_file):
    ctx, tab, _conn = build(tmp_path, key=key_file)
    ctx.storages.add(Storage(name="Ноутбук", kind="pc", location=str(FIXTURES)))
    tab.refresh()
    tab.where.setCurrentIndex(1)
    settle(ctx)
    tab.table.selectRow(0)

    tab.verify()

    assert any("на сервере" in e for e in ctx.errors)


# ---- a new server ------------------------------------------------------------


def test_a_new_server_is_brought_up_from_a_snapshot(tmp_path, key_file):
    ctx, tab, conn = build(tmp_path, key=key_file)
    tab.table.selectRow(0)

    tab.recover()
    settle(ctx)

    seen = commands(conn)
    steps = [c for c in seen if c.startswith(("holdfast restore", "cd /root/myapp"))]
    # the snapshot is checked first, before the question and before any write
    assert "restore verify" in steps[0]
    assert "--component project-myapp" in steps[1] and "--no-stop" in steps[1]
    assert steps[2] == "cd /root/myapp && docker compose up -d"  # no -p
    assert steps[-1].endswith("--yes") and "--component" not in steps[-1]
    # the folder asked about is the project's own, not the recipe's "/"
    assert any("/root/myapp" in text for _, text in ctx.confirmed)


# ---- copying -----------------------------------------------------------------


def test_a_snapshot_is_copied_to_a_folder_on_this_computer(tmp_path, key_file):
    ctx, tab, _conn = build(tmp_path, key=key_file)
    target = tmp_path / "pcbackups"
    ctx.storages.add(Storage(name="Ноутбук", kind="pc", location=str(target)))
    ctx.choose = lambda title, options: 0
    tab.table.selectRow(0)

    tab.copy_to()
    settle(ctx)

    assert (target / "web-1" / "20260921-020000" / "manifest.json").is_file()
    assert actionlog.read(ctx.log_path)[0].ok


def test_a_snapshot_is_sent_to_a_remote_without_deleting_anything(tmp_path, key_file):
    ctx, tab, conn = build(tmp_path, key=key_file)
    ctx.storages.add(
        Storage(name="Хранилище 1", kind="remote", location="shared:backups")
    )
    ctx.choose = lambda title, options: 0
    tab.table.selectRow(0)

    tab.copy_to()
    settle(ctx)

    sent = next(c for c in commands(conn) if c.startswith("rclone copy"))
    assert "shared:backups/web-1/20260921-020000" in sent
    assert "sync" not in sent and "delete" not in sent


def test_copying_without_any_storage_explains_what_to_do(tmp_path):
    ctx, tab, _ = build(tmp_path)
    tab.table.selectRow(0)
    tab.copy_to()
    assert any("Хранилища" in e for e in ctx.errors)


# ---- the storages dialog -----------------------------------------------------


def test_the_storages_dialog_adds_lists_and_removes(tmp_path):
    ctx = make_ctx(tmp_path)
    dialog = StoragesDialog(ctx)

    dialog.name.setText("Ноутбук")
    dialog.kind.setCurrentIndex(0)
    dialog.location.setText("D:/backups")
    dialog.add_storage()
    dialog.name.setText("Хранилище 1")
    dialog.kind.setCurrentIndex(1)
    dialog.location.setText("shared:backups")
    dialog.add_storage()

    assert dialog.listing.count() == 2
    assert [s.kind for s in ctx.storages.load()] == ["pc", "remote"]

    dialog.name.setText("Ноутбук")
    dialog.location.setText("E:/x")
    dialog.add_storage()
    assert "already exists" in dialog.message.text() and dialog.listing.count() == 2

    dialog.listing.setCurrentRow(0)
    dialog.remove_storage()
    assert [s.name for s in ctx.storages.load()] == ["Хранилище 1"]


def test_the_first_place_is_always_the_server():
    assert SERVER == "server"


# ---- an old answer must not overwrite a newer one --------------------------------


class Slow(FakeConn):
    """Holds the server listing until released, as a slow link would."""

    hold = None

    def run(self, command, timeout=60):
        if self.hold is not None and "for d in /opt/backups" in command:
            self.hold.wait(10)
        return super().run(command, timeout)


def test_a_slow_old_listing_does_not_overwrite_the_newer_one(tmp_path):
    import threading
    import time

    ctx, tab, conn = build(tmp_path, conn=Slow(answers()))
    ctx.storages.add(Storage(name="Ноутбук", kind="pc", location=str(FIXTURES)))
    tab._fill_where()
    conn.hold = threading.Event()

    tab.refresh()  # the server listing: held back
    tab.where.setCurrentIndex(1)  # then the folder here: answers at once
    deadline = time.monotonic() + 5
    while tab.table.rowCount() != 1 and time.monotonic() < deadline:
        application().processEvents()
        time.sleep(0.01)
    assert tab.table.rowCount() == 1

    conn.hold.set()  # now the old answer (2 snapshots) arrives
    settle(ctx)

    assert tab.table.rowCount() == 1
    assert tab.current_place()[0] == "pc"
