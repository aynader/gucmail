"""Accepted reader identity, persisted timing and stale/cancelled write safety."""

import asyncio
from dataclasses import replace
from threading import Event, get_ident
from time import perf_counter

import pytest
from textual.widgets import Button, DataTable, Input, Static

import gucmail.tui.app as app_module
from gucmail.errors import ConfigurationError
from gucmail.models import ItemSource, MessageDetail
from gucmail.settings import SettingsService
from gucmail.tui.app import ConfirmationScreen, GUCmailApp, ReaderScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_tui import FakeGateway, context_for


class ReadGateway(FakeGateway):
    def __init__(self):
        self.loaded = []
        self.marked = []
        self.block_load = Event()
        self.load_started = Event()
        self.block_write = Event()
        self.write_started = Event()
        self.fail_write = False
        self.source = ItemSource.LIVE

    def list_messages(
        self, *, folder, limit, offset: int = 0, order_by: str = "-datetime_received"
    ):
        first = super().list_messages(folder="inbox", limit=limit)[0]
        return (first, replace(first, reference="m-other", subject="Other synthetic message"))

    def show_message(self, reference, *, mark_read=False):
        self.loaded.append((reference, mark_read))
        self.load_started.set()
        assert self.block_load.wait(5)
        summary = next(
            m for m in self.list_messages(folder="inbox", limit=2) if m.reference == reference
        )
        return MessageDetail(replace(summary, source=self.source), body_text="Synthetic text")

    def mark_read(self, reference, read=True):
        self.marked.append((reference, read, get_ident()))
        self.write_started.set()
        assert self.block_write.wait(5)
        if self.fail_write:
            raise OSError("Private body or credentials must never appear")


async def wait_for(pilot, condition, *, attempts=150):
    for _ in range(attempts):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError("Reader workflow did not reach the expected state")


def configured(tmp_path, delay=0.4):
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("mail.mark_read_on_open", True)
    settings.set("mail.mark_read_delay_seconds", delay)
    gateway = ReadGateway()
    gateway.block_load.set()
    gateway.block_write.set()
    return context, gateway, GUCmailApp(context, gateway=gateway)


@pytest.mark.parametrize("invalid", [-0.01, 301, True, "nan", "inf", "invalid"])
def test_invalid_delay_preserves_persisted_configuration(tmp_path, invalid):
    context = context_for(tmp_path)
    original = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set("mail.mark_read_delay_seconds", invalid)
    assert context.paths.config_file.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,size", [("pane", (120, 35)), ("full", (70, 20)), ("floating", (80, 24))]
)
async def test_delay_marks_only_the_displayed_target_off_event_loop(
    tmp_path, monkeypatch, mode, size
):
    context, gateway, app = configured(tmp_path, 0.8)
    ui_thread = get_ident()
    scheduled_at, written_at = [], []
    schedule = app._schedule_automatic_read
    mark = gateway.mark_read

    def recorded_schedule(*args):
        started_at = perf_counter()
        schedule(*args)
        if app._automatic_read_timer is not None:
            scheduled_at.append(started_at)

    def recorded_mark(*args, **kwargs):
        written_at.append(perf_counter())
        return mark(*args, **kwargs)

    monkeypatch.setattr(app, "_schedule_automatic_read", recorded_schedule)
    monkeypatch.setattr(gateway, "mark_read", recorded_mark)
    async with app.run_test(size=size) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        await asyncio.wait_for(app.workers.wait_for_complete(), 5)
        await pilot.pause()
        app.load_message("m-demo", mode=mode)
        await wait_for(
            pilot,
            lambda: app._message_detail_reference == "m-demo" and app.ui.busy is None,
            attempts=500,
        )
        await wait_for(
            pilot,
            lambda: (
                bool(gateway.marked)
                and not app._automatic_read_inflight
                and app._message_detail.summary.is_read
            ),
            attempts=500,
        )
        # The delay is measured at the write boundary. A loaded host can run the
        # timer before Pilot resumes; observing a transient timer is unnecessary.
        assert len(scheduled_at) == 1 and len(written_at) == 1
        assert written_at[0] - scheduled_at[0] >= 0.8
        assert gateway.loaded == [("m-demo", False)]
        assert [(ref, read) for ref, read, _ in gateway.marked] == [("m-demo", True)]
        assert gateway.marked[0][2] != ui_thread
        assert app._message_detail.summary.is_read
        persisted = SettingsService(context.config_store)
        assert persisted.value("mail.mark_read_delay_seconds").effective_value == 0.8
        assert persisted.export()["settings"]["mail.mark_read_delay_seconds"] == 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["none", "selection", "replacement"])
async def test_floating_reader_waits_for_mount_and_rejects_a_stale_mount_result(
    tmp_path, monkeypatch, abandon
):
    _context, gateway, app = configured(tmp_path, 0)
    started, permitted = asyncio.Event(), asyncio.Event()

    mount = app.mount

    def held_mount(*widgets, **kwargs):
        completion = mount(*widgets, **kwargs)
        if not any(isinstance(widget, app_module.FloatingReader) for widget in widgets):
            return completion

        async def acknowledged():
            await completion
            started.set()
            await permitted.wait()

        # Hold the public mount acknowledgement without blocking the App's
        # widget/message pumps; concurrent selection must remain possible.
        return asyncio.create_task(acknowledged())

    monkeypatch.setattr(app, "mount", held_mount)
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
            app.load_message("m-demo", mode="floating")
            await asyncio.wait_for(started.wait(), 3)
            assert not gateway.marked and app._automatic_read_request is None
            if abandon != "none":
                app.query_one("#message-table", DataTable).move_cursor(row=1)
                for _ in range(100):
                    if app.ui.selected_reference == "m-other":
                        break
                    await asyncio.sleep(0.01)
                assert app.ui.selected_reference == "m-other"
                if abandon == "replacement":
                    app.load_message("m-other", mode="floating")
                    for _ in range(100):
                        if app._message_detail_reference == "m-other":
                            break
                        await asyncio.sleep(0.01)
                    assert app._message_detail_reference == "m-other"
            permitted.set()
            if abandon == "selection":
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not gateway.marked
            else:
                await wait_for(
                    pilot, lambda: bool(gateway.marked) and not app._automatic_read_inflight
                )
                expected = "m-other" if abandon == "replacement" else "m-demo"
                assert [reference for reference, _read, _thread in gateway.marked] == [expected]
                if abandon == "replacement":
                    assert app.query_one(
                        "#floating-reader", app_module.FloatingReader
                    ).reader_title == ("Other synthetic message")
        finally:
            permitted.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("abandon", ["selection", "close", "cover", "disable", "resize", "folder"])
async def test_abandoned_reader_never_marks_a_message(tmp_path, abandon):
    context, gateway, app = configured(tmp_path, 0.8)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode="full" if abandon == "close" else "pane")
        await wait_for(pilot, lambda: app._automatic_read_timer is not None)
        if abandon == "selection":
            app.query_one("#message-table", DataTable).move_cursor(row=1)
            await wait_for(pilot, lambda: app.ui.selected_reference == "m-other")
        elif abandon == "close":
            assert isinstance(app.screen, ReaderScreen)
            await pilot.press("escape")
        elif abandon == "cover":
            await app.push_screen(ConfirmationScreen("Synthetic modal", "Read-only cover"))
            await pilot.pause()
            await pilot.press("escape")
        elif abandon == "disable":
            SettingsService(context.config_store).set("mail.mark_read_on_open", False)
            app._apply_committed_settings(("mail.mark_read_on_open",))
        elif abandon == "resize":
            await pilot.resize_terminal(70, 20)
            await pilot.resize_terminal(120, 35)
        else:
            app._activate_folder_location("Sent")
        await pilot.pause(0.9)
        assert not gateway.marked


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["cancel", "timeout", "supersede"])
async def test_late_loads_are_read_only_and_cannot_start_a_read_change(tmp_path, cancel):
    _context, gateway, app = configured(tmp_path, 0)
    gateway.block_load.clear()
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode="pane")
        await wait_for(pilot, gateway.load_started.is_set)
        if cancel == "cancel":
            app.action_navigate_back()
        elif cancel == "timeout":
            app._message_load_timed_out(app._message_generation)
        else:
            app.query_one("#message-table", DataTable).move_cursor(row=1)
            await wait_for(pilot, lambda: app.ui.selected_reference == "m-other")
        gateway.block_load.set()
        await pilot.pause(0.3)
        assert gateway.loaded == [("m-demo", False)]
        assert not gateway.marked


@pytest.mark.asyncio
async def test_accepted_write_can_finish_after_selection_changes_without_retargeting(tmp_path):
    _context, gateway, app = configured(tmp_path, 0)
    gateway.block_write.clear()
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode="pane")
        await wait_for(pilot, gateway.write_started.is_set)
        app.query_one("#message-table", DataTable).move_cursor(row=1)
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-other")
        gateway.block_write.set()
        await wait_for(pilot, lambda: not app._automatic_read_inflight)
        assert [(ref, read) for ref, read, _ in gateway.marked] == [("m-demo", True)]
        assert app.ui.selected_reference == "m-other"
        states = {item.reference: item.is_read for item in app._visible_messages}
        assert states == {"m-demo": True, "m-other": False}


@pytest.mark.asyncio
async def test_queued_automatic_worker_rechecks_selection_before_any_gateway_write(
    tmp_path, monkeypatch
):
    _context, gateway, app = configured(tmp_path, 0)
    queued = []
    execute = app._automatic_mark_read_worker
    monkeypatch.setattr(app, "_automatic_mark_read_worker", queued.append)
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode="pane")
        await wait_for(pilot, lambda: bool(queued))
        app.query_one("#message-table", DataTable).move_cursor(row=1)
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-other")
        worker = execute(queued[0])
        await worker.wait()
        assert not gateway.marked
        assert not app._automatic_read_inflight


@pytest.mark.asyncio
async def test_failed_automatic_write_shows_uncertainty_and_never_replays_on_rerender(tmp_path):
    _context, gateway, app = configured(tmp_path, 0)
    gateway.fail_write = True
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode="pane")
        await wait_for(pilot, lambda: bool(gateway.marked) and not app._automatic_read_inflight)
        status = str(app.query_one("#status-bar", Static).content)
        assert "may have completed" in status
        assert "Private body" not in status
        app._render_message(app._message_detail, mode="pane")
        await pilot.pause(0.2)
        assert len(gateway.marked) == 1
        assert not app._message_detail.summary.is_read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,source",
    [("details", ItemSource.LIVE), ("pane", ItemSource.BACKUP), ("pane", ItemSource.CACHE)],
)
async def test_details_and_non_live_content_do_not_change_read_state(tmp_path, mode, source):
    _context, gateway, app = configured(tmp_path, 0)
    gateway.source = source
    async with app.run_test(size=(120, 35)) as pilot:
        await wait_for(pilot, lambda: app.ui.selected_reference == "m-demo")
        app.load_message("m-demo", mode=mode)
        await wait_for(pilot, lambda: app._message_detail is not None)
        await pilot.pause(0.2)
        assert not gateway.marked


@pytest.mark.asyncio
async def test_normal_settings_stage_apply_and_restart_preserve_timing(tmp_path):
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        screen = app.screen
        assert screen.action_select_setting("mail.mark_read_delay_seconds")
        screen.query_one("#settings-editor", Input).value = "2.5"
        screen.query_one("#settings-stage", Button).press()
        await wait_for(pilot, lambda: "mail.mark_read_delay_seconds" in screen._staged)
        assert (
            SettingsService(context.config_store)
            .value("mail.mark_read_delay_seconds")
            .effective_value
            == 0
        )
        screen.query_one("#settings-apply", Button).press()
        await wait_for(pilot, lambda: not screen._staged and not screen.mutation_pending)
        reopened = GUCmailApp(context, offline=True)
        assert reopened._setting_value("mail.mark_read_delay_seconds", -1) == 2.5
