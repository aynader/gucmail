"""Named layout lifecycle, atomic stale-preview rejection and mounted normal entry points."""

import copy
import json
from dataclasses import replace

import pytest
from textual.widgets import Button, Input
from typer.testing import CliRunner

import gucmail.cli as cli
from gucmail.commands import CommandInvocation
from gucmail.errors import ConfigurationError, ConflictError
from gucmail.services.layout_presets import LayoutPresetService
from gucmail.settings import InMemoryRevisionStore, PolicyLock, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.layout_presets_screen import LayoutPresetsScreen
from gucmail.tui.palette_routes import catalog_route
from test_cli import _environment
from test_compose_preferences import wait_for
from test_settings_secondpass import _context


def test_named_presets_save_load_restart_delete_export_import_reset_and_undo(tmp_path):
    context = _context(tmp_path)
    settings = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    service = LayoutPresetService(settings)
    catalog = service.catalog()
    values = copy.deepcopy(catalog.current)
    values.update(
        {
            "layout.sidebar_ratio": 0.33,
            "layout.preview_visible": False,
            "layout.header_contents": ["folder", "account"],
            "layout.reader_mode": "ask",
        }
    )
    service.save("Reading 界", values, catalog=catalog)
    assert settings.value("layout.sidebar_ratio").effective_value == 0.22
    with pytest.raises(ConflictError, match="already exists"):
        service.save("READING 界", catalog.current, catalog=service.catalog())
    service.load("READING 界", catalog=service.catalog())
    restarted = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert restarted._sidebar_ratio == 0.33 and not restarted._preview_visible
    assert restarted._reader_mode == "ask" and restarted._header_contents == ("folder", "account")
    assert restarted._layout_last_preset == "saved:Reading 界"
    exported = settings.export()
    assert set(exported["settings"]["layout.presets"]["Reading 界"]) == set(values)
    assert "email" not in json.dumps(exported["settings"]["layout.presets"])
    target = SettingsService(_context(tmp_path / "target").config_store)
    target.apply_import(target.plan_import(exported))
    assert LayoutPresetService(target).catalog().presets == service.catalog().presets
    service.delete("Reading 界", catalog=service.catalog())
    assert not service.catalog().presets
    assert settings.value("layout.sidebar_ratio").effective_value == 0.33
    settings.undo()
    assert "Reading 界" in service.catalog().presets
    settings.reset("layout.presets")
    assert not service.catalog().presets


@pytest.mark.parametrize("operation", ["save", "load", "delete"])
def test_stale_catalog_never_overwrites_newer_configuration(tmp_path, operation):
    context = _context(tmp_path)
    settings = SettingsService(context.config_store)
    service = LayoutPresetService(settings)
    first = service.catalog()
    service.save("First", first.current, catalog=first)
    stale = service.catalog()
    settings.set("layout.header_compactness", "detailed")
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConflictError, match="changed"):
        if operation == "save":
            service.save("Second", stale.current, catalog=stale)
        else:
            getattr(service, operation)("First", catalog=stale)
    assert context.paths.config_file.read_bytes() == before


@pytest.mark.parametrize(
    "mutation", ["extra", "missing", "invalid", "duplicate", "control", "oversized"]
)
def test_invalid_imported_preset_fails_before_any_config_write(tmp_path, mutation):
    context = _context(tmp_path)
    service = LayoutPresetService(SettingsService(context.config_store))
    data = {"First": service.catalog().current}
    if mutation == "extra":
        data["First"]["composer.signature"] = "Not a layout field"
    elif mutation == "missing":
        data["First"].pop("layout.message_columns")
    elif mutation == "invalid":
        data["First"]["layout.sidebar_ratio"] = 100
    elif mutation == "duplicate":
        data["FIRST"] = copy.deepcopy(data["First"])
    elif mutation == "control":
        data["Bad\x1b[2J"] = data.pop("First")
    else:
        data = {f"Layout {n}": copy.deepcopy(data["First"]) for n in range(33)}
    before = context.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        service.settings.set("layout.presets", data)
    assert context.paths.config_file.read_bytes() == before


def test_locked_load_and_failed_save_are_atomic(tmp_path, monkeypatch):
    context = _context(tmp_path)
    service = LayoutPresetService(SettingsService(context.config_store))
    catalog = service.catalog()
    service.save("First", catalog.current, catalog=catalog)
    before = context.paths.config_file.read_bytes()
    locked = LayoutPresetService(
        SettingsService(
            context.config_store,
            policy_locks={"layout.sidebar_ratio": PolicyLock("Synthetic policy")},
        )
    )
    with pytest.raises(ConfigurationError, match="locked"):
        locked.load("First", catalog=locked.catalog())
    assert context.paths.config_file.read_bytes() == before

    def denied(*_args, **_kwargs):
        raise OSError("Synthetic storage failure")

    monkeypatch.setattr(context.config_store, "save_document", denied)
    with pytest.raises(OSError):
        service.delete("First", catalog=service.catalog())
    assert context.paths.config_file.read_bytes() == before


def test_layout_cli_preview_lifecycle_and_explicit_replacement_stay_local(tmp_path, monkeypatch):
    context = _context(tmp_path)
    monkeypatch.setattr(
        cli.core, "gateway", lambda *_: pytest.fail("Layouts must not authenticate")
    )
    runner = CliRunner()

    def invoke(*args):
        result = runner.invoke(cli.app, ["--json", *args], env=_environment(tmp_path))
        return result

    assert invoke("layouts", "--help").exit_code == 0
    assert invoke("layouts", "save", "Local", "--dry-run").exit_code == 0
    assert not LayoutPresetService(SettingsService(context.config_store)).catalog().presets
    assert invoke("layouts", "save", "Local").exit_code == 0
    assert invoke("layouts", "save", "local").exit_code == 9
    assert invoke("layouts", "save", "local", "--replace").exit_code == 0
    SettingsService(context.config_store).set("layout.preview_visible", False)
    before = context.paths.config_file.read_bytes()
    assert invoke("layouts", "load", "Local", "--dry-run").exit_code == 0
    assert context.paths.config_file.read_bytes() == before
    assert invoke("layouts", "load", "Local").exit_code == 0
    assert SettingsService(context.config_store).value("layout.preview_visible").effective_value
    assert invoke("layouts", "delete", "Local", "--dry-run").exit_code == 0
    assert invoke("layouts", "delete", "Local").exit_code == 9
    assert invoke("--yes", "layouts", "delete", "Local").exit_code == 0
    assert not LayoutPresetService(SettingsService(context.config_store)).catalog().presets


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_palette_save_load_replace_cancel_delete_and_restart(
    tmp_path, monkeypatch, size
):
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(app, "_ensure_gateway", lambda: pytest.fail("Layout UI is local"))
    async with app.run_test(size=size) as pilot:
        app._sidebar_ratio = 0.30
        app._preview_visible = False
        route = catalog_route("layout.save_layout_preset")
        app._catalog_application_action(route, CommandInvocation(route.command_id))
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, LayoutPresetsScreen) and app.screen.catalog is not None,
        )
        screen = app.screen
        await pilot.pause()
        for button in screen.query("#layout-presets-actions Button"):
            assert button.region.width > 0
            assert 0 <= button.region.x < button.region.right <= size[0]
            assert 0 <= button.region.y < button.region.bottom <= size[1]
        screen.query_one("#layout-presets-name", Input).value = "Synthetic Reading"
        screen.query_one("#layout-presets-save", Button).focus()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                screen.catalog is not None
                and "Synthetic Reading" in screen.catalog.presets
                and not screen._operation_active
            ),
        )
        save_button = screen.query_one("#layout-presets-save", Button)
        await wait_for(pilot, lambda: not save_button.has_class("-active"))
        save_button.focus()
        await pilot.press("enter")
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        before = context.paths.config_file.read_bytes()
        app.screen.dismiss(False)
        await pilot.pause()
        assert context.paths.config_file.read_bytes() == before
        await pilot.press("escape")
        await pilot.pause()
        app.action_reset_layout()
        assert app._preview_visible and app._sidebar_ratio == 0.22
        load = catalog_route("layout.load_layout_preset")
        app._catalog_application_action(load, CommandInvocation(load.command_id))
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, LayoutPresetsScreen) and app.screen.catalog is not None,
        )
        screen = app.screen
        await pilot.pause()
        screen.query_one("#layout-presets-load", Button).focus()
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: (
                app._sidebar_ratio == 0.30
                and not screen._operation_active
                and screen.view_state is ViewState.READY
            ),
        )
        assert not app._preview_visible and app._layout_last_preset == "saved:Synthetic Reading"
        restarted = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
        assert restarted._sidebar_ratio == 0.30 and not restarted._preview_visible
        screen.query_one("#layout-presets-delete", Button).press()
        await wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmationScreen) and app.screen.is_mounted
        )
        app.screen.dismiss(True)
        await wait_for(
            pilot,
            lambda: (
                screen.catalog is not None
                and not screen.catalog.presets
                and not screen._operation_active
            ),
        )
        assert app._sidebar_ratio == 0.30 and not app._preview_visible
