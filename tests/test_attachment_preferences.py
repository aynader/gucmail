"""Persisted download choices and exact collision safety using synthetic files only."""

import io
import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, Select, Static
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.errors import ConfigurationError, ConfirmationRequiredError, ConflictError
from gucmail.models import AttachmentInfo, DraftData
from gucmail.services.attachment_preferences import AttachmentPreferences
from gucmail.services.attachments import AttachmentService, CollisionPolicy
from gucmail.services.drafts import DraftService
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.drafts_attachments_backend import ServiceDraftAttachmentBackend
from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen
from gucmail.tui.settings_screen import SettingsScreen
from test_cli import _environment
from test_compose_preferences import wait_for
from test_settings_secondpass import _context
from test_tui import FakeGateway


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
@pytest.mark.parametrize("accept", [False, True])
async def test_long_confirmation_keeps_mouse_controls_visible_and_details_keyboard_scrollable(
    size, accept
):
    app = App()
    results = []
    async with app.run_test(size=size) as pilot:
        screen = ConfirmationScreen(
            "Overwrite existing attachment files?",
            "The following exact paths already exist:\n"
            + "\n".join(f"- C:/synthetic/long-folder-{index}/report.txt" for index in range(30)),
            confirm_label="Overwrite files",
        )
        await app.push_screen(screen, results.append)
        await pilot.pause()
        body = screen.query_one("#operational-confirm-body", VerticalScroll)
        assert body.max_scroll_y > 0
        body.focus()
        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert body.scroll_y > 0
        for identifier in ["operational-confirm-no", "operational-confirm-yes"]:
            button = screen.query_one(f"#{identifier}", Button)
            assert button.region.bottom <= screen.size.height
            assert button.region.height == 3
        clicked = await pilot.click(
            "#operational-confirm-yes" if accept else "#operational-confirm-no"
        )
        assert clicked
        await pilot.pause()
        assert results == [accept] and screen not in app.screen_stack


class DownloadGateway(FakeGateway):
    def __init__(self, names=("report.txt",)):
        self.metadata_calls = []
        self.opened = []
        self.infos = tuple(
            AttachmentInfo(f"att-{index}", name, 3, "text/plain")
            for index, name in enumerate(names)
        )

    def list_attachments(self, message_reference, limit=100, offset=0):
        self.metadata_calls.append((message_reference, limit, offset))
        return self.infos[offset : offset + limit]

    def open_attachment(self, message_reference, attachment_reference):
        self.opened.append((message_reference, attachment_reference))
        return io.BytesIO(b"new")


def test_collision_preferences_restart_transfer_reset_undo_and_account_scope(tmp_path):
    context = _context(tmp_path)
    service = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    assert AttachmentPreferences.from_settings(service).collision is CollisionPolicy.RENAME
    session = service.begin()
    session.set("mail.attachment_collision", "error")
    session.apply()
    assert (
        AttachmentPreferences.from_settings(SettingsService(context.config_store)).collision
        is CollisionPolicy.ERROR
    )
    target = SettingsService(_context(tmp_path / "target").config_store)
    target.apply_import(target.plan_import(service.export()))
    assert AttachmentPreferences.from_settings(target).collision is CollisionPolicy.ERROR
    service.reset("mail.attachment_collision")
    assert AttachmentPreferences.from_settings(service).collision is CollisionPolicy.RENAME
    service.undo()
    assert AttachmentPreferences.from_settings(service).collision is CollisionPolicy.ERROR
    first = SettingsService(context.config_store, context=SettingsContext(account_id="first"))
    second = SettingsService(context.config_store, context=SettingsContext(account_id="second"))
    first.set("mail.attachment_collision", "overwrite")
    assert AttachmentPreferences.from_settings(first).collision is CollisionPolicy.OVERWRITE
    assert AttachmentPreferences.from_settings(second).collision is CollisionPolicy.ERROR


@pytest.mark.parametrize("value", ["replace", True, 1, ["rename"]])
def test_invalid_collision_setting_preserves_configuration(tmp_path, value):
    context = _context(tmp_path)
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        SettingsService(context.config_store).set("mail.attachment_collision", value)
    assert context.paths.config_file.read_bytes() == before


def test_invalid_collision_service_override_never_reads_gateway(tmp_path):
    gateway = DownloadGateway()
    service = AttachmentService(gateway)
    with pytest.raises(ConfigurationError):
        service.plan_save("message", "att-0", tmp_path, collision="replace")
    with pytest.raises(ConfigurationError):
        service.plan_save_all("message", tmp_path, collision="replace")
    assert not gateway.metadata_calls and not gateway.opened


@pytest.mark.parametrize("policy", [CollisionPolicy.ERROR, CollisionPolicy.OVERWRITE])
def test_duplicate_batch_destinations_fail_before_content_or_files(tmp_path, policy):
    gateway = DownloadGateway(("../../report.txt", "report.txt"))
    service = AttachmentService(gateway, collision_policy=policy)
    with pytest.raises(ConflictError, match="Multiple attachments"):
        service.save_all("message", tmp_path / "destination")
    assert not gateway.opened
    assert not (tmp_path / "destination").exists()


def test_rename_batch_reserves_distinct_normalized_paths_without_creating_them(tmp_path):
    gateway = DownloadGateway(("../../report.txt", "report.txt", "report (1).txt"))
    service = AttachmentService(gateway)
    plans = service.plan_save_all("message", tmp_path)
    assert [plan.target.name for plan in plans] == [
        "report.txt",
        "report (1).txt",
        "report (1) (1).txt",
    ]
    assert not gateway.opened and not list(tmp_path.iterdir())
    results = service.save_all("message", tmp_path, calculate_sha256=True)
    assert len({result.path for result in results}) == 3
    assert all(result.path.read_bytes() == b"new" and result.sha256 for result in results)


def test_overwrite_batch_preflight_blocks_before_an_earlier_noncollision_download(tmp_path):
    (tmp_path / "existing.txt").write_bytes(b"old")
    gateway = DownloadGateway(("first.txt", "existing.txt"))
    service = AttachmentService(gateway, collision_policy="overwrite")
    preview = service.save_all("message", tmp_path, dry_run=True)
    assert preview[1].plan.requires_confirmation
    with pytest.raises(ConfirmationRequiredError):
        service.save_all("message", tmp_path)
    assert not gateway.opened
    assert not (tmp_path / "first.txt").exists()
    assert (tmp_path / "existing.txt").read_bytes() == b"old"


@pytest.mark.parametrize("when", ["before_download", "while_streaming"])
@pytest.mark.parametrize("hard_links", [True, False])
def test_unreviewed_new_file_is_never_overwritten_by_saved_policy(
    tmp_path, when, hard_links, monkeypatch
):
    if not hard_links:
        monkeypatch.setattr(os, "link", lambda *_: (_ for _ in ()).throw(OSError("Unavailable")))
    gateway = DownloadGateway()
    service = AttachmentService(gateway, collision_policy="overwrite")
    target = tmp_path / "report.txt"
    plan = service.plan_save("message", "att-0", tmp_path)
    assert plan.overwrite and not plan.requires_confirmation
    if when == "before_download":
        target.write_bytes(b"another process")
    else:
        original = gateway.open_attachment

        def open_with_race(*args):
            target.write_bytes(b"another process")
            return original(*args)

        gateway.open_attachment = open_with_race
    with pytest.raises(ConflictError):
        service.execute_save(plan)
    assert target.read_bytes() == b"another process"
    assert len(gateway.opened) == (0 if when == "before_download" else 1)
    assert not list(tmp_path.glob(".*.part"))


def test_changed_target_resolution_is_rejected_before_download(tmp_path, monkeypatch):
    gateway = DownloadGateway()
    service = AttachmentService(gateway)
    plan = service.plan_save("message", "att-0", tmp_path)
    other = tmp_path / "another.txt"
    original = Path.resolve

    def changed_path(path, *args, **kwargs):
        return other if path == plan.target else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", changed_path)
    with pytest.raises(ConflictError, match="target changed"):
        service.execute_save(plan)
    assert not gateway.opened and not other.exists()


@pytest.mark.parametrize("policy", list(CollisionPolicy))
def test_destination_symlink_is_rejected_without_content(tmp_path, monkeypatch, policy):
    gateway = DownloadGateway()
    service = AttachmentService(gateway, collision_policy=policy)
    target = tmp_path / "report.txt"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    with pytest.raises(ConfigurationError, match="symlink"):
        service.plan_save("message", "att-0", tmp_path)
    assert not gateway.opened


def test_cli_saved_collision_explicit_override_preview_and_confirmed_overwrite(
    tmp_path, monkeypatch
):
    context = _context(tmp_path)
    SettingsService(context.config_store).set("mail.attachment_collision", "overwrite")
    destination = tmp_path / "downloads"
    destination.mkdir()
    target = destination / "report.txt"
    target.write_bytes(b"old")
    gateway = DownloadGateway()
    monkeypatch.setattr(cli, "_gateway", lambda _: gateway)
    runner = CliRunner()
    args = ["--json", "attachments", "save", "message", "att-0", "--directory", str(destination)]
    preview = runner.invoke(cli.app, [*args, "--dry-run"], env=_environment(tmp_path))
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["data"]["plan"]["requires_confirmation"]
    rejected = runner.invoke(cli.app, args, env=_environment(tmp_path))
    assert rejected.exit_code != 0 and "--yes" in rejected.output
    assert not gateway.opened and target.read_bytes() == b"old"
    rename = runner.invoke(cli.app, [*args, "--collision", "rename"], env=_environment(tmp_path))
    assert rename.exit_code == 0, rename.output
    assert target.read_bytes() == b"old"
    assert (destination / "report (1).txt").read_bytes() == b"new"
    saved = runner.invoke(cli.app, ["--yes", *args, "--sha256"], env=_environment(tmp_path))
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.output)["data"]["sha256"]
    assert target.read_bytes() == b"new"
    assert (
        AttachmentPreferences.from_settings(SettingsService(context.config_store)).collision
        is CollisionPolicy.OVERWRITE
    )


def test_cli_save_all_preflights_every_path_and_bound_before_content(tmp_path, monkeypatch):
    context = _context(tmp_path)
    SettingsService(context.config_store).set("mail.attachment_collision", "overwrite")
    destination = tmp_path / "downloads"
    destination.mkdir()
    (destination / "existing.txt").write_bytes(b"old")
    gateway = DownloadGateway(("first.txt", "existing.txt"))
    monkeypatch.setattr(cli, "_gateway", lambda _: gateway)
    runner = CliRunner()
    args = ["--json", "attachments", "save-all", "message", "--directory", str(destination)]
    preview = runner.invoke(cli.app, [*args, "--dry-run"], env=_environment(tmp_path))
    assert preview.exit_code == 0, preview.output
    assert len(json.loads(preview.output)["data"]) == 2
    rejected = runner.invoke(cli.app, args, env=_environment(tmp_path))
    assert rejected.exit_code != 0
    assert not gateway.opened and not (destination / "first.txt").exists()
    saved = runner.invoke(cli.app, ["--yes", *args, "--sha256"], env=_environment(tmp_path))
    assert saved.exit_code == 0, saved.output
    assert all(item["sha256"] for item in json.loads(saved.output)["data"])
    assert gateway.metadata_calls[-1] == ("message", 100, 0)
    monkeypatch.setattr(cli, "_gateway", lambda _: pytest.fail("Invalid options stay local"))
    invalid = runner.invoke(cli.app, [*args, "--limit", "201"], env=_environment(tmp_path))
    assert invalid.exit_code == 2
    invalid = runner.invoke(cli.app, [*args, "--collision", "replace"], env=_environment(tmp_path))
    assert invalid.exit_code == 2


def local_backend(tmp_path):
    context = _context(tmp_path)
    files = []
    for name in ["one", "two"]:
        folder = tmp_path / name
        folder.mkdir()
        path = folder / "report.txt"
        path.write_bytes(name.encode())
        files.append(path)
    drafts = DraftService(context.state, context.paths.recovery_dir)
    drafts.create(DraftData("dft-download", attachment_paths=files))
    backend = ServiceDraftAttachmentBackend(drafts, online=False)
    return backend


def test_local_batch_reserves_distinct_paths_and_rejects_duplicate_overwrites(tmp_path):
    backend = local_backend(tmp_path)
    destination = tmp_path / "downloads"
    kwargs = dict(limit=10, cancel=Event(), progress=lambda _: None)
    renamed = backend.prepare_all_attachment_saves(
        "dft-download", destination, collision="rename", **kwargs
    )
    assert [plan.destination.name for plan in renamed] == ["report.txt", "report (1).txt"]
    assert not destination.exists()
    with pytest.raises(ConflictError, match="Multiple attachments"):
        backend.prepare_all_attachment_saves(
            "dft-download", destination, collision="overwrite", **kwargs
        )
    saved = backend.execute_attachment_saves(
        renamed, confirmed=False, calculate_sha256=True, cancel=Event(), progress=lambda _: None
    )
    assert [item.path.read_bytes() for item in saved] == [b"one", b"two"]


def test_server_workspace_batch_prepares_one_bounded_metadata_page(tmp_path):
    context = _context(tmp_path)
    gateway = DownloadGateway(("report.txt", "report.txt"))
    backend = ServiceDraftAttachmentBackend(
        DraftService(context.state, context.paths.recovery_dir),
        attachments=AttachmentService(gateway),
    )
    plans = backend.prepare_all_attachment_saves(
        "message",
        tmp_path / "downloads",
        collision="rename",
        limit=10,
        cancel=Event(),
        progress=lambda _: None,
    )
    assert gateway.metadata_calls == [("message", 10, 0)]
    assert not gateway.opened
    assert [plan.destination.name for plan in plans] == ["report.txt", "report (1).txt"]


@pytest.mark.parametrize("when", ["before_copy", "while_copying"])
def test_local_unreviewed_new_target_never_receives_overwrite(tmp_path, when, monkeypatch):
    backend = local_backend(tmp_path)
    destination = tmp_path / "downloads"
    snapshot = backend.list_attachments(
        "dft-download", query="", limit=1, cancel=Event(), progress=lambda _: None
    )
    plan = backend.prepare_attachment_save(
        "dft-download",
        snapshot.attachments[0].reference,
        destination,
        collision="overwrite",
        expected_sha256=None,
        cancel=Event(),
        progress=lambda _: None,
    )
    destination.mkdir()
    if when == "before_copy":
        plan.destination.write_bytes(b"another process")
    else:
        original = backend._copy_stream

        def copy_with_race(*args):
            plan.destination.write_bytes(b"another process")
            return original(*args)

        monkeypatch.setattr(backend, "_copy_stream", copy_with_race)
    with pytest.raises(ConflictError):
        backend.execute_attachment_saves(
            (plan,), confirmed=False, calculate_sha256=True, cancel=Event(), progress=lambda _: None
        )
    assert plan.destination.read_bytes() == b"another process"
    assert not list(destination.glob(".*.part"))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_settings_collision_stage_cancel_apply_and_workspace_confirmation(tmp_path, size):
    context = _context(tmp_path)
    service = SettingsService(context.config_store)
    gateway = DownloadGateway()
    app = GUCmailApp(context, gateway=gateway)
    destination = tmp_path / "downloads"
    destination.mkdir()
    target = destination / "report.txt"
    target.write_bytes(b"old")
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await wait_for(
            pilot, lambda: isinstance(app.screen, SettingsScreen) and app.screen._catalog
        )
        settings = app.screen
        before = context.paths.config_file.read_bytes()
        assert settings.action_select_setting("mail.attachment_collision")
        await pilot.pause()
        settings.query_one("#settings-choice", Select).value = "overwrite"
        settings.query_one("#settings-stage", Button).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: settings._staged and not settings._operation_active)
        assert context.paths.config_file.read_bytes() == before
        assert not gateway.opened
        settings.action_cancel_changes()
        await wait_for(
            pilot, lambda: not settings._staged and settings.view_state is ViewState.READY
        )
        assert service.value("mail.attachment_collision").effective_value == "rename"
        assert settings.action_select_setting("mail.attachment_collision")
        await pilot.pause()
        settings.query_one("#settings-choice", Select).value = "overwrite"
        stage = settings.query_one("#settings-stage", Button)
        await wait_for(pilot, lambda: not stage.has_class("-active"))
        stage.focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: settings._staged and not settings._operation_active)
        await pilot.press("ctrl+s")
        await wait_for(
            pilot, lambda: not settings._staged and settings.view_state is ViewState.READY
        )
        await pilot.press("escape")
        app._open_draft_attachment_workspace("message")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, DraftAttachmentScreen) and app.screen._attachments,
        )
        screen = app.screen
        assert screen.query_one("#attachment-collision", Select).value == "overwrite"
        screen.query_one("#attachment-destination").value = str(destination)
        screen.query_one("#attachment-save", Button).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        assert str(target) in app.screen.detail_text
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.screen is screen and not screen._operation_active)
        assert target.read_bytes() == b"old" and not gateway.opened
        await wait_for(
            pilot, lambda: not screen.query_one("#attachment-save", Button).has_class("-active")
        )
        screen.query_one("#attachment-save", Button).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await wait_for(
            pilot,
            lambda: (
                app.screen is screen
                and "Saved 1" in str(screen.query_one("#attachment-detail", Static).content)
            ),
        )
        assert target.read_bytes() == b"new" and len(gateway.opened) == 1
    restarted = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert (
        AttachmentPreferences.from_settings(restarted._settings_service()).collision
        is CollisionPolicy.OVERWRITE
    )


@pytest.mark.asyncio
async def test_applied_default_updates_existing_workspace_and_preserves_download_override(tmp_path):
    context = _context(tmp_path)
    app = GUCmailApp(context, gateway=DownloadGateway())
    async with app.run_test() as pilot:
        app._open_draft_attachment_workspace("message")
        await wait_for(
            pilot, lambda: isinstance(app.screen, DraftAttachmentScreen) and app.screen._attachments
        )
        screen = app.screen
        service = app._settings_service()
        service.set("mail.attachment_collision", "error")
        app._apply_committed_settings(("mail.attachment_collision",))
        await pilot.pause()
        assert screen.query_one("#attachment-collision", Select).value == "error"
        screen.query_one("#attachment-collision", Select).value = "rename"
        await pilot.pause()
        assert screen._collision_overridden
        service.set("mail.attachment_collision", "overwrite")
        app._apply_committed_settings(("mail.attachment_collision",))
        await pilot.pause()
        assert screen.query_one("#attachment-collision", Select).value == "rename"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["rename", "error", "overwrite"])
async def test_workspace_bulk_duplicate_names_complete_or_reject_before_writes(tmp_path, policy):
    context = _context(tmp_path)
    SettingsService(context.config_store).set("mail.attachment_collision", policy)
    gateway = DownloadGateway(("report.txt", "report.txt"))
    app = GUCmailApp(context, gateway=gateway)
    destination = tmp_path / "downloads"
    async with app.run_test(size=(80, 24)) as pilot:
        app._open_draft_attachment_workspace("message")
        await wait_for(
            pilot,
            lambda: (
                isinstance(app.screen, DraftAttachmentScreen) and len(app.screen._attachments) == 2
            ),
        )
        screen = app.screen
        screen.query_one("#attachment-destination").value = str(destination)
        screen.query_one("#attachment-save-all", Button).focus()
        await pilot.press("enter")
        if policy == "rename":
            await wait_for(
                pilot,
                lambda: (destination / "report (1).txt").exists() and not screen._operation_active,
            )
            assert len(gateway.opened) == 2
            assert (destination / "report.txt").read_bytes() == b"new"
        else:
            await wait_for(
                pilot, lambda: screen.view_state is ViewState.ERROR and not screen._operation_active
            )
            assert app.screen is screen and not gateway.opened and not destination.exists()
            assert "Multiple attachments" in str(screen.query_one("#area-state", Static).content)
