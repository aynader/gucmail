from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from tests.test_config_atomic_snapshots import _store
from tests.test_tui import _ready_composer_for_editing, context_for
from textual.widgets import Button, DataTable, Input, Select, Static
from textual.worker import WorkerState

from gucmail.errors import ConfigurationError, ConflictError
from gucmail.services.keyboard_bindings import (
    KEYBOARD_SETTING,
    KEYBOARD_SETTING_IDS,
    keyboard_catalog,
    normalize_binding_overrides,
    validate_binding_configuration,
)
from gucmail.services.keyboard_preferences import KeyboardPreferencesService
from gucmail.services.shortcuts import SHORTCUT_DEFAULTS
from gucmail.settings import InMemoryRevisionStore, SettingsContext, SettingsService
from gucmail.tui.app import ComposeScreen, GUCmailApp, HelpScreen
from gucmail.tui.area_common import ConfirmationScreen, OperationalScreen, ViewState
from gucmail.tui.keyboard_editor import KeyboardEditorScreen
from gucmail.tui.settings_screen import SettingsHistoryScreen, SettingsScreen
from waiting import rounds, wait_until


def _binding(owner: str, action: str):
    return next(
        row
        for row in keyboard_catalog()[0]
        if row.context.endswith("." + owner) and row.action == action
    )


def test_catalog_preserves_fixed_clipboard_and_close_bindings() -> None:
    definitions, fixed = keyboard_catalog()
    assert len({row.binding_id for row in definitions}) == len(definitions)
    assert all(
        row.action not in {"screen.copy_text", "close", "cancel", "back"} for row in definitions
    )
    assert any(row.action == "screen.copy_text" for row in fixed)
    assert any(row.key == "escape" for row in fixed)
    assert validate_binding_configuration({}, SHORTCUT_DEFAULTS) == {}


def test_packaged_catalog_definitions_and_fixed_controls_match_actual_class_metadata() -> None:
    definitions, fixed = keyboard_catalog()
    for row in definitions:
        module, name = row.context.rsplit(".", 1)
        owner = getattr(importlib.import_module(module), name)
        candidates = owner._merged_bindings.key_to_bindings[row.default_key]
        assert any(
            binding.action == row.action
            and binding.description == row.description
            and binding.priority == row.priority
            and binding.show == row.show
            for binding in candidates
        )
    for row in fixed:
        module, name = row.context.rsplit(".", 1)
        owner = getattr(importlib.import_module(module), name)
        assert any(
            binding.action == row.action
            for binding in owner._merged_bindings.key_to_bindings[row.key]
        )


def test_keyboard_scoped_transfer_is_complete_bounded_and_revision_checked(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(store.load())
    settings = SettingsService(store, revisions=InMemoryRevisionStore())
    service = KeyboardPreferencesService(settings)
    compose = _binding("GUCmailApp", "compose")
    settings.set(KEYBOARD_SETTING, {compose.binding_id: "f8"})
    path = tmp_path / "keyboard.json"
    service.export(path)
    payload = json.loads(path.read_text())
    assert set(payload["settings"]) == set(KEYBOARD_SETTING_IDS)
    settings.set(KEYBOARD_SETTING, {})
    snapshot, plan = service.preview_import_state(path)
    assert snapshot.revision == plan.expected_revision
    assert settings.value(KEYBOARD_SETTING).configured_value == {}
    service.apply_import(plan)
    assert settings.value(KEYBOARD_SETTING).configured_value == {compose.binding_id: "f8"}
    settings.set(KEYBOARD_SETTING, {})
    stale = service.preview_import(path)
    settings.set("appearance.density", "compact")
    before = store.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError, match="changed after this import preview"):
        service.apply_import(stale)
    assert store.paths.config_file.read_bytes() == before
    original_export = path.read_bytes()
    with pytest.raises(ConflictError, match="already exists"):
        service.export(path)
    assert path.read_bytes() == original_export
    for fields in [
        {KEYBOARD_SETTING: {}},
        {**payload["settings"], "mail.mark_read_on_open": True},
    ]:
        path.write_text(json.dumps({**payload, "settings": fields}))
        with pytest.raises(ConfigurationError, match="no other settings"):
            service.preview_import(path)
        assert store.paths.config_file.read_bytes() == before


@pytest.mark.parametrize("key", ["alt+f4", "alt+tab", "alt+space", "ctrl+alt+delete"])
def test_native_system_control_combinations_are_reserved(key: str) -> None:
    with pytest.raises(ConfigurationError, match="reserved"):
        normalize_binding_overrides({_binding("GUCmailApp", "compose").binding_id: key})


@pytest.mark.parametrize("raw", [[], None, True, "unknown", {"opaque.action": "f8"}])
def test_override_schema_rejects_unknown_actions_and_non_mappings(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        normalize_binding_overrides(raw)


@pytest.mark.parametrize("key", ["ctrl+s", "ctrl+v", "ctrl+shift+c", "f6", "f7", "enter", "f8,f9"])
def test_remaps_preserve_text_editing_and_key_dispatch_safety(key: str) -> None:
    identifier = _binding("GUCmailApp", "compose").binding_id
    with pytest.raises(ConfigurationError):
        validate_binding_configuration({identifier: key}, SHORTCUT_DEFAULTS)


def test_aliases_cross_context_reuse_and_atomic_conflicts() -> None:
    compose = _binding("GUCmailApp", "compose")
    reply = _binding("GUCmailApp", "reply")
    help_action = _binding("HelpScreen", "tutorials")
    assert validate_binding_configuration(
        {compose.binding_id: "F8", help_action.binding_id: "f8"}, SHORTCUT_DEFAULTS
    ) == {compose.binding_id: "f8", help_action.binding_id: "f8"}
    with pytest.raises(ConfigurationError, match="different actions"):
        validate_binding_configuration(
            {compose.binding_id: "f8", reply.binding_id: "f8"}, SHORTCUT_DEFAULTS
        )
    with pytest.raises(ConfigurationError, match="global shortcut"):
        validate_binding_configuration({compose.binding_id: "ctrl+p"}, SHORTCUT_DEFAULTS)
    with pytest.raises(ConfigurationError, match="global shortcut"):
        validate_binding_configuration(
            {compose.binding_id: "f8"}, {**SHORTCUT_DEFAULTS, "palette.primary_shortcut": "f8"}
        )


def test_disable_and_default_restore_need_no_dangling_override() -> None:
    compose = _binding("GUCmailApp", "compose")
    assert normalize_binding_overrides({compose.binding_id: ""}) == {compose.binding_id: ""}
    assert normalize_binding_overrides({compose.binding_id: " C "}) == {}


def test_staged_mapping_transfer_reset_undo_and_concurrent_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(store.load())
    service = SettingsService(store, revisions=InMemoryRevisionStore())
    compose = _binding("GUCmailApp", "compose")
    mapping = {compose.binding_id: "f8"}
    before = store.paths.config_file.read_bytes()
    stage = service.begin().set(KEYBOARD_SETTING, mapping)
    stage.cancel()
    assert store.paths.config_file.read_bytes() == before
    service.set(KEYBOARD_SETTING, mapping)
    assert SettingsService(_store(tmp_path)).value(KEYBOARD_SETTING).configured_value == mapping
    exported = service.export(setting_ids=KEYBOARD_SETTING_IDS)
    assert set(exported["settings"]) == set(KEYBOARD_SETTING_IDS)
    service.reset_category("Keyboard")
    assert service.value(KEYBOARD_SETTING).configured_value == {}
    service.undo()
    assert service.value(KEYBOARD_SETTING).configured_value == mapping
    plan = service.plan_import(
        {**exported, "settings": {**exported["settings"], KEYBOARD_SETTING: {}}}
    )
    service.apply_import(plan)
    assert service.value(KEYBOARD_SETTING).configured_value == {}
    stale = service.begin().set(KEYBOARD_SETTING, mapping)
    service.set("appearance.density", "compact")
    newer = store.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError, match="changed in another process"):
        stale.apply()
    assert store.paths.config_file.read_bytes() == newer


def test_keyboard_preferences_keep_global_scope_across_account_machine_and_profile(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save(store.load())
    first = SettingsService(
        store,
        context=SettingsContext(account_id="synthetic-a", machine_id="host-a", profile="work"),
    )
    compose = _binding("GUCmailApp", "compose").binding_id
    first.set(KEYBOARD_SETTING, {compose: "f8"})
    first.set("palette.secondary_shortcut", "")
    second = SettingsService(
        _store(tmp_path),
        context=SettingsContext(account_id="synthetic-b", machine_id="host-b", profile="personal"),
    )
    assert second.value(KEYBOARD_SETTING).configured_value == {compose: "f8"}
    assert second.value("palette.secondary_shortcut").configured_value == ""
    document = store.load_document()
    assert KEYBOARD_SETTING in document.settings
    assert all(
        "keyboard.bindings" not in key for key in document.settings if key != KEYBOARD_SETTING
    )


def test_invalid_environment_mapping_fails_candidate_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.save(store.load())
    service = SettingsService(store)
    compose = _binding("GUCmailApp", "compose")
    reply = _binding("GUCmailApp", "reply")
    monkeypatch.setenv(
        "GUCMAIL_SETTING_KEYBOARD__BINDINGS",
        json.dumps({compose.binding_id: "f8", reply.binding_id: "f8"}),
    )
    before = store.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError, match="different actions"):
        service.set("palette.primary_shortcut", "f9")
    assert store.paths.config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_root_remap_preserves_typing_scope_palette_and_restores_default(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    compose = _binding("GUCmailApp", "compose")
    help_action = _binding("GUCmailApp", "help")
    service.set(KEYBOARD_SETTING, {compose.binding_id: "f8", help_action.binding_id: "f9"})
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        await pilot.pause(0.02)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("f9")
        await _visible(pilot, app, "#help-shortcuts")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape", "escape")
        app.query_one("#message-table", DataTable).focus()
        await pilot.press("c")
        assert not isinstance(app.screen, ComposeScreen)
        await pilot.press("f8")
        assert isinstance(app.screen, ComposeScreen)
        screen = await _ready_composer_for_editing(pilot)
        field = screen.query(Input).first()
        field.focus()
        await pilot.press("h", "e", "l", "l", "o", "f8")
        assert field.value == "hello"
        assert app.screen is screen
        assert app.check_action("compose", ()) is False
        assert app.check_action("help", ()) is True
        await pilot.press("f9")
        # Modal dispatch retains the original non-priority binding boundary.
        assert app.screen is screen and field.value == "hello"
        await pilot.press("ctrl+k")
        assert app.screen is not screen
        await pilot.press("escape")
        assert app.screen is screen
        service.set(KEYBOARD_SETTING, {})
        app._apply_committed_settings((KEYBOARD_SETTING,))
        assert "f8" not in app._bindings.key_to_bindings
        assert any(binding.action == "compose" for binding in app._bindings.key_to_bindings["c"])
        assert any(
            binding.action == "close" for binding in screen._bindings.key_to_bindings["escape"]
        )


@pytest.mark.asyncio
async def test_mounted_and_future_screen_bindings_update_without_class_mutation(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    tutorials = _binding("HelpScreen", "tutorials")
    original = HelpScreen._merged_bindings.copy()
    service = SettingsService(context.config_store)
    async with app.run_test() as pilot:
        app.action_help()
        await pilot.pause()
        current = app.screen
        assert isinstance(current, HelpScreen)
        service.set(KEYBOARD_SETTING, {tutorials.binding_id: "f8"})
        app._apply_committed_settings((KEYBOARD_SETTING,))
        assert any(
            binding.action == "tutorials" for binding in current._bindings.key_to_bindings["f8"]
        )
        assert not any(
            binding.action == "tutorials"
            for binding in current._bindings.key_to_bindings.get(tutorials.default_key, [])
        )
        await pilot.press("escape")
        app.action_help()
        await pilot.pause()
        assert app.screen is not current
        assert "f8" in app.screen._bindings.key_to_bindings
        service.set(KEYBOARD_SETTING, {})
        app._apply_committed_settings((KEYBOARD_SETTING,))
        assert "f8" not in app.screen._bindings.key_to_bindings
        assert HelpScreen._merged_bindings.key_to_bindings == original.key_to_bindings


async def _ready(pilot, screen: KeyboardEditorScreen) -> None:
    for _ in rounds(250):
        await pilot.pause(0.02)
        worker = screen._active_worker
        if worker is not None and worker.state is WorkerState.RUNNING:
            try:
                await asyncio.wait_for(asyncio.shield(worker.wait()), timeout=10)
            except TimeoutError:
                break
        if screen.snapshot is not None and not screen._operation_active:
            return
        if (
            screen.snapshot is None
            and not screen._operation_active
            and screen.view_state is ViewState.ERROR
        ):
            break
    status = str(screen.query_one("#area-state", Static).render())
    pytest.fail(
        f"Keyboard editor did not complete its local operation: {screen.view_state}; {status}"
    )


async def _open_editor(pilot, app: GUCmailApp) -> KeyboardEditorScreen:
    await pilot.press("?")
    await _visible(pilot, app, "#help-shortcuts")
    assert await pilot.click("#help-shortcuts")
    await _visible(pilot, app, "#shortcut-editor")
    assert await pilot.click("#shortcut-editor")
    return await _editor_opened(pilot, app)


async def _editor_opened(pilot, app: GUCmailApp) -> KeyboardEditorScreen:
    await wait_until(
        pilot,
        lambda: isinstance(app.screen, KeyboardEditorScreen),
        interval=0.02,
        pause_first=True,
    )
    editor = app.screen
    assert isinstance(editor, KeyboardEditorScreen)
    await _ready(pilot, editor)
    return editor


async def _visible(pilot, app: GUCmailApp, identifier: str) -> None:
    for _ in rounds(100):
        await pilot.pause(0.02)
        matches = app.screen.query(identifier)
        if matches:
            widget = matches.first()
            if (
                widget.is_mounted
                and widget.region.height > 0
                and app.screen.region.contains_region(widget.region)
            ):
                return
    pytest.fail(f"Normal workflow control did not become visible: {identifier}")


async def _activate(pilot, screen: OperationalScreen, identifier: str) -> None:
    if isinstance(screen, SettingsScreen):
        if (
            identifier in {"#settings-managed", "#settings-reset-setting"}
            and not screen._editor_open
        ):
            screen.action_edit_setting()
            await pilot.pause()
        if identifier in {"#settings-history", "#settings-refresh", "#settings-transfer"} and not (
            screen.query_one("#settings-advanced-actions").has_class("visible")
        ):
            screen.query_one("#settings-more", Button).press()
            await pilot.pause()
    button = screen.query_one(identifier, Button)
    assert not button.disabled
    button.focus()

    def shown() -> bool:
        if button.region.intersection(screen.region).height == button.region.height:
            return not button.has_class("-active")
        # A scroll asked for before the layout settled can be lost; ask again.
        button.scroll_visible(animate=False)
        return False

    await wait_until(pilot, shown, interval=0.02, pause_first=True)
    assert button.region.intersection(screen.region).height == button.region.height
    await pilot.press("enter")
    await pilot.pause()


async def _stage(pilot, editor: KeyboardEditorScreen, identifier: str, key: str) -> None:
    editor.query_one("#keyboard-search", Input).value = identifier
    await pilot.pause()
    assert editor.selected == identifier
    field = editor.query_one("#keyboard-key", Input)
    field.value = key
    field.focus()
    await pilot.press("ctrl+enter")


async def _confirm(pilot, app: GUCmailApp, accepted: bool) -> None:
    for _ in rounds(100):
        await pilot.pause(0.02)
        screen = app.screen
        if isinstance(screen, ConfirmationScreen) and screen.query("#operational-confirm-yes"):
            button = screen.query_one("#operational-confirm-yes", Button)
            if button.is_mounted:
                break
    assert isinstance(app.screen, ConfirmationScreen)
    await pilot.press("y" if accepted else "escape")
    await pilot.pause(0.02)
    assert not isinstance(app.screen, ConfirmationScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_editor_rejects_conflicting_partial_swap_then_applies_complete_swap(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    compose = _binding("GUCmailApp", "compose").binding_id
    reply = _binding("GUCmailApp", "reply").binding_id
    service.set(KEYBOARD_SETTING, {compose: "f8", reply: "f9"})
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        editor = await _open_editor(pilot, app)
        # Entering Help legitimately persists its recent-page history.
        before = context.paths.config_file.read_bytes()
        await _stage(pilot, editor, compose, "f9")
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert editor.view_state is ViewState.ERROR
        assert "different actions" in str(editor.query_one("#area-state", Static).render())
        assert context.paths.config_file.read_bytes() == before
        assert editor.staged[KEYBOARD_SETTING] == {compose: "f9", reply: "f9"}
        await _stage(pilot, editor, reply, "f8")
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {compose: "f9", reply: "f8"}
        assert not editor.staged
        assert any(row.action == "compose" for row in app._bindings.key_to_bindings["f9"])
        assert any(row.action == "reply" for row in app._bindings.key_to_bindings["f8"])


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_editor_export_reset_import_confirmations_and_cancellation(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    service = SettingsService(context.config_store)
    compose = _binding("GUCmailApp", "compose").binding_id
    service.set(KEYBOARD_SETTING, {compose: "f8"})
    service.set("palette.secondary_shortcut", "")
    path = tmp_path / "keys.json"
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=size) as pilot:
        editor = await _open_editor(pilot, app)
        before = context.paths.config_file.read_bytes()
        editor.query_one("#keyboard-path", Input).value = str(path)
        await _activate(pilot, editor, "#keyboard-export")
        await _ready(pilot, editor)
        payload = path.read_bytes()
        assert set(json.loads(payload)["settings"]) == set(KEYBOARD_SETTING_IDS)
        assert context.paths.config_file.read_bytes() == before
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, False)
        assert not editor.staged
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, True)
        assert set(editor.staged) == set(KEYBOARD_SETTING_IDS)
        assert context.paths.config_file.read_bytes() == before
        await pilot.press("ctrl+z")
        assert not editor.staged
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, True)
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {}
        assert service.value("palette.secondary_shortcut").configured_value == "ctrl+p"
        reset_bytes = context.paths.config_file.read_bytes()
        await _activate(pilot, editor, "#keyboard-export")
        await _confirm(pilot, app, False)
        assert path.read_bytes() == payload
        await _activate(pilot, editor, "#keyboard-import")
        await _ready(pilot, editor)
        assert editor.staged[KEYBOARD_SETTING] == {compose: "f8"}
        assert context.paths.config_file.read_bytes() == reset_bytes
        await pilot.press("ctrl+z")
        assert editor.import_plan is None
        assert editor.query_one("#keyboard-apply-import", Button).disabled
        await _activate(pilot, editor, "#keyboard-import")
        await _ready(pilot, editor)
        await pilot.press("ctrl+s")
        await _confirm(pilot, app, False)
        assert context.paths.config_file.read_bytes() == reset_bytes
        assert editor.import_plan is not None
        await _activate(pilot, editor, "#keyboard-apply-import")
        await _confirm(pilot, app, True)
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {compose: "f8"}
        assert service.value("palette.secondary_shortcut").configured_value == ""
        assert not editor.staged and editor.import_plan is None
        assert path.read_bytes() == payload
        status = editor.query_one("#area-state", Static)
        assert "Keyboard assignments saved" in str(status.render())
        assert editor.region.contains_region(status.region)


@pytest.mark.asyncio
async def test_editor_resize_preserves_staged_selection_and_visible_assignment_columns(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    compose = _binding("GUCmailApp", "compose").binding_id
    async with app.run_test(size=(120, 35)) as pilot:
        editor = await _open_editor(pilot, app)
        before = context.paths.config_file.read_bytes()
        await _stage(pilot, editor, compose, "f8")
        for width, height in [(80, 24), (70, 20), (120, 35)]:
            await pilot.resize_terminal(width, height)
            await pilot.pause(0.1)
            table = editor.query_one("#keyboard-table", DataTable)
            assert editor.selected == compose
            assert editor.staged[KEYBOARD_SETTING] == {compose: "f8"}
            assert table.row_count == 1
            assert [str(column.label) for column in table.columns.values()] == [
                "Area",
                "Action",
                "Current",
                "Proposed",
            ]
            assert sum(column.width + 2 for column in table.columns.values()) <= width - 4
            assert editor.region.contains_region(editor.query_one("#area-state", Static).region)
            assert context.paths.config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["", "\x00", "directory"])
async def test_keyboard_file_path_errors_remain_in_editor(tmp_path: Path, invalid: str) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(70, 20)) as pilot:
        editor = await _open_editor(pilot, app)
        before = context.paths.config_file.read_bytes()
        editor.query_one("#keyboard-path", Input).value = (
            str(tmp_path) if invalid == "directory" else invalid
        )
        for identifier in ("#keyboard-export", "#keyboard-import"):
            await _activate(pilot, editor, identifier)
            assert app.screen is editor and editor.view_state is ViewState.ERROR
            assert "valid file path" in str(editor.query_one("#area-state", Static).render())
            assert not editor._operation_active
            assert context.paths.config_file.read_bytes() == before


@pytest.mark.asyncio
async def test_environment_locked_assignments_survive_reset_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    compose = _binding("GUCmailApp", "compose").binding_id
    monkeypatch.setenv("GUCMAIL_SETTING_KEYBOARD__BINDINGS", json.dumps({compose: "f8"}))
    service = SettingsService(context.config_store)
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(70, 20)) as pilot:
        editor = await _open_editor(pilot, app)
        editor.query_one("#keyboard-search", Input).value = compose
        await pilot.pause()
        assert editor.query_one("#keyboard-key", Input).disabled
        assert editor.query_one("#keyboard-stage", Button).disabled
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, True)
        assert KEYBOARD_SETTING not in editor.staged
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {}
        assert service.value(KEYBOARD_SETTING).effective_value == {compose: "f8"}
        assert "f8" in app._bindings.key_to_bindings


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"removed.screen:old_action": "f8"}, ["f8"], False])
async def test_saved_invalid_action_map_requires_explicit_revision_checked_repair(
    tmp_path: Path, invalid: object
) -> None:
    context = context_for(tmp_path)
    document = context.config_store.load_document()
    context.config_store.save_document(
        document.stored_config,
        {**document.settings, KEYBOARD_SETTING: invalid},
        expected_revision=document.revision,
    )
    service = SettingsService(context.config_store, revisions=InMemoryRevisionStore())
    assert KeyboardPreferencesService(service).snapshot().warning
    with pytest.raises(ConfigurationError):
        service.value(KEYBOARD_SETTING)
    app = GUCmailApp(context, offline=True)
    assert app._binding_overrides == {} and app._shortcut_notice
    async with app.run_test(size=(70, 20)) as pilot:
        editor = await _open_editor(pilot, app)
        assert "Saved action assignments are invalid" in str(
            editor.query_one("#area-state", Static).render()
        )
        before = context.paths.config_file.read_bytes()
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, False)
        assert context.paths.config_file.read_bytes() == before
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, True)
        assert context.paths.config_file.read_bytes() == before
        # A valid concurrent edit must win over this previously reviewed reset.
        service.set("appearance.density", "compact")
        newer = context.paths.config_file.read_bytes()
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert editor.view_state is ViewState.ERROR
        assert context.paths.config_file.read_bytes() == newer
        await pilot.press("ctrl+z")
        await _activate(pilot, editor, "#keyboard-refresh")
        await _ready(pilot, editor)
        await _activate(pilot, editor, "#keyboard-reset-all")
        await _confirm(pilot, app, True)
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {}
        assert app._binding_overrides == {} and not app._shortcut_notice
        assert not editor.snapshot.warning


@pytest.mark.parametrize(
    "content",
    [b"{", b"\xff", b" " * (4 * 1024 * 1024 + 1)],
    ids=["malformed-json", "invalid-encoding", "over-limit"],
)
def test_keyboard_import_rejects_invalid_or_oversized_file_without_change(
    tmp_path: Path, content: bytes
) -> None:
    store = _store(tmp_path)
    store.save(store.load())
    service = KeyboardPreferencesService(SettingsService(store))
    path = tmp_path / "invalid-keys.json"
    path.write_bytes(content)
    before = store.paths.config_file.read_bytes()
    with pytest.raises(ConfigurationError):
        service.preview_import(path)
    assert store.paths.config_file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_help_keyboard_editor_stages_cancels_applies_and_refreshes_browser(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    compose = _binding("GUCmailApp", "compose")
    service = SettingsService(context.config_store)
    async with app.run_test(size=size) as pilot:
        editor = await _open_editor(pilot, app)
        browser = app.screen_stack[-2]
        editor.query_one("#keyboard-search", Input).value = compose.binding_id
        await pilot.pause()
        assert editor.query_one("#keyboard-table", DataTable).row_count == 1
        assert editor.selected == compose.binding_id
        before = context.paths.config_file.read_bytes()
        editor.query_one("#keyboard-key", Input).value = "f8"
        editor.query_one("#keyboard-key", Input).focus()
        await pilot.press("ctrl+enter")
        assert editor.staged[KEYBOARD_SETTING] == {compose.binding_id: "f8"}
        assert context.paths.config_file.read_bytes() == before
        await pilot.press("ctrl+z")
        assert not editor.staged
        assert context.paths.config_file.read_bytes() == before
        editor.query_one("#keyboard-key", Input).value = "f8"
        editor.query_one("#keyboard-key", Input).focus()
        await pilot.press("ctrl+enter", "ctrl+s")
        await _ready(pilot, editor)
        assert service.value(KEYBOARD_SETTING).configured_value == {compose.binding_id: "f8"}
        assert not editor.staged
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is browser
        current = next(row for row in browser.records if row.shortcut_id == compose.binding_id)
        assert current.key == "f8" and current.customizable
        await pilot.press("escape", "escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, HelpScreen))
        await pilot.press("f8")
        await wait_until(pilot, lambda: isinstance(app.screen, ComposeScreen))
        assert isinstance(app.screen, ComposeScreen)


@pytest.mark.asyncio
async def test_closing_shortcuts_twice_closes_only_the_shortcuts_screen(tmp_path: Path) -> None:
    # A double click on Close asks the screen to close twice. Only it may close, not the
    # help screen underneath.
    from gucmail.tui.shortcuts_screen import ShortcutsScreen

    app = GUCmailApp(context_for(tmp_path), offline=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("?")
        await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
        help_screen = app.screen
        app._open_shortcuts()
        await wait_until(
            pilot,
            lambda: isinstance(app.screen, ShortcutsScreen) and app.screen.query("#shortcut-close"),
        )
        await pilot.pause()
        shortcuts = app.screen
        assert isinstance(shortcuts, ShortcutsScreen)
        shortcuts.action_close()
        shortcuts.action_close()
        await pilot.pause()
        assert app.screen is help_screen


async def _settled(pilot, screen: OperationalScreen, predicate) -> None:
    for _ in rounds(250):
        await pilot.pause(0.02)
        if not screen._operation_active and predicate():
            return
    pytest.fail(f"Settings workflow did not complete: {screen.view_state}")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_settings_entry_editor_dirty_close_single_reset_and_history_restore(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    service = SettingsService(context.config_store)
    compose = _binding("GUCmailApp", "compose").binding_id
    async with app.run_test(size=size) as pilot:
        app._open_area("settings")
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        await _settled(pilot, settings, lambda: settings._catalog is not None)
        assert settings.action_select_setting(KEYBOARD_SETTING)
        await pilot.pause()
        assert not settings.query_one("#settings-editor", Input).display
        assert str(settings.query_one("#settings-managed", Button).label) == "Edit keys"
        await _activate(pilot, settings, "#settings-managed")
        editor = await _editor_opened(pilot, app)
        before = context.paths.config_file.read_bytes()
        await _stage(pilot, editor, compose, "f8")
        await pilot.press("escape")
        await _confirm(pilot, app, False)
        assert app.screen is editor and editor.staged
        assert context.paths.config_file.read_bytes() == before
        await _activate(pilot, editor, "#keyboard-reset-key")
        assert editor.staged[KEYBOARD_SETTING] == {}
        assert context.paths.config_file.read_bytes() == before
        await _stage(pilot, editor, compose, "f8")
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        await pilot.press("escape")
        await _settled(
            pilot,
            settings,
            lambda: settings._catalog.revision == context.config_store.load_document().revision,
        )
        assert app.screen is settings
        assert service.value(KEYBOARD_SETTING).configured_value == {compose: "f8"}
        await _activate(pilot, settings, "#settings-history")
        history = app.screen
        assert isinstance(history, SettingsHistoryScreen)
        await _settled(pilot, history, lambda: bool(history._revisions))
        assert history._selected_revision is not None
        assert (
            KEYBOARD_SETTING in history._revisions[history._selected_revision].changed_setting_ids
        )
        changed = context.paths.config_file.read_bytes()
        await _activate(pilot, history, "#settings-restore-revision")
        await _confirm(pilot, app, False)
        assert context.paths.config_file.read_bytes() == changed
        await _activate(pilot, history, "#settings-restore-revision")
        await _confirm(pilot, app, True)
        await _settled(
            pilot, history, lambda: service.value(KEYBOARD_SETTING).configured_value == {}
        )
        assert "f8" not in app._bindings.key_to_bindings
        assert any(row.action == "compose" for row in app._bindings.key_to_bindings["c"])


@pytest.mark.asyncio
async def test_keyboard_commit_preserves_independent_staged_settings_and_discard_close(
    tmp_path: Path,
) -> None:
    context = context_for(tmp_path)
    app = GUCmailApp(context, offline=True)
    service = SettingsService(context.config_store)
    compose = _binding("GUCmailApp", "compose").binding_id
    original_density = service.value("appearance.density").configured_value
    async with app.run_test(size=(70, 20)) as pilot:
        app._open_area("settings")
        settings = app.screen
        assert isinstance(settings, SettingsScreen)
        await _settled(pilot, settings, lambda: settings._catalog is not None)
        assert settings.action_select_setting("appearance.density")
        settings.query_one("#settings-choice", Select).value = "compact"
        await _activate(pilot, settings, "#settings-stage")
        await _settled(pilot, settings, lambda: bool(settings._staged))
        assert settings._staged == {"appearance.density": "compact"}
        assert settings.action_select_setting(KEYBOARD_SETTING)
        await _activate(pilot, settings, "#settings-managed")
        editor = await _editor_opened(pilot, app)
        before = context.paths.config_file.read_bytes()
        await _stage(pilot, editor, compose, "f8")
        await pilot.press("escape")
        await _confirm(pilot, app, True)
        assert app.screen is settings
        assert context.paths.config_file.read_bytes() == before
        assert settings._staged == {"appearance.density": "compact"}
        await _activate(pilot, settings, "#settings-managed")
        editor = await _editor_opened(pilot, app)
        await _stage(pilot, editor, compose, "f8")
        await pilot.press("ctrl+s")
        await _ready(pilot, editor)
        await pilot.press("escape")
        await pilot.pause(0.02)
        assert app.screen is settings
        assert settings._staged == {"appearance.density": "compact"}
        assert "Your staged preferences remain" in str(
            settings.query_one("#area-state", Static).render()
        )
        saved = context.paths.config_file.read_bytes()
        await pilot.press("ctrl+s")
        await _settled(pilot, settings, lambda: settings.view_state is ViewState.ERROR)
        assert context.paths.config_file.read_bytes() == saved
        await pilot.press("ctrl+z")
        await _settled(pilot, settings, lambda: not settings._staged)
        await _activate(pilot, settings, "#settings-refresh")
        await _settled(
            pilot,
            settings,
            lambda: settings._catalog.revision == context.config_store.load_document().revision,
        )
        assert service.value(KEYBOARD_SETTING).configured_value == {compose: "f8"}
        assert service.value("appearance.density").configured_value == original_density
