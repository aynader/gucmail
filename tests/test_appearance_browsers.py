from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from textual.app import App
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea

from gucmail.tui.appearance_screen import (
    ThemeBrowserBackend,
    ThemeBrowserScreen,
    ThemeEditorScreen,
)
from gucmail.tui.branding import (
    BrandAlignment,
    BrandColorBehavior,
    BrandingPreferences,
)
from gucmail.tui.branding_screen import BrandingBrowserBackend, BrandingBrowserScreen
from gucmail.tui.theme_repository import CustomThemeRepository
from gucmail.tui.themes import (
    ColorCapability,
    ContrastClass,
    ThemePreferences,
    ThemeRegistry,
    ValidationSeverity,
    adapt_theme_high_contrast,
    contrast_ratio,
)
from waiting import wait_until


class _ScreenHost(App[None]):
    def __init__(self, screen: Screen[None]) -> None:
        super().__init__()
        self._screen_to_push = screen

    def get_default_screen(self) -> Screen[None]:
        return self._screen_to_push


def _theme_backend(
    tmp_path: Path,
    *,
    theme_preferences: ThemePreferences | None = None,
    persisted: list[ThemePreferences] | None = None,
    previewed: list[str] | None = None,
) -> ThemeBrowserBackend:
    return ThemeBrowserBackend(
        CustomThemeRepository(tmp_path / "appearance" / "custom-themes.json"),
        preferences=theme_preferences,
        persist_preferences=None if persisted is None else persisted.append,
        on_preview=None if previewed is None else lambda theme: previewed.append(theme.id),
        on_apply=None if previewed is None else lambda theme: previewed.append(f"apply:{theme.id}"),
        on_cancel=(
            None if previewed is None else lambda theme: previewed.append(f"cancel:{theme.id}")
        ),
    )


def test_theme_browser_backend_search_categories_staged_session_and_monochrome(
    tmp_path: Path,
) -> None:
    persisted: list[ThemePreferences] = []
    callbacks: list[str] = []
    backend = _theme_backend(tmp_path, persisted=persisted, previewed=callbacks)

    snapshot = backend.browse()
    assert len(snapshot.entries) == 661
    assert ("Favorites", "favorites") in snapshot.categories
    assert ("GUC / Germany", "guc-germany") in snapshot.categories
    assert backend.browse(query="defalt drk").entries[0].name == "Default Dark"
    for _, category in snapshot.categories:
        if category not in {"favorites", "recently-used", "custom"}:
            assert backend.browse(category=category).entries, category
    assert all(
        "guc-germany" in backend.registry.get(entry.theme_id).categories
        for entry in backend.browse(category="guc-germany").entries
    )

    original = backend.session.applied
    preview = backend.preview("builtin.default-light")
    assert preview.theme.id == "builtin.default-light"
    assert backend.session.applied == original
    assert callbacks[-1] == "builtin.default-light"
    cancelled = backend.cancel_preview()
    assert cancelled.theme == original
    assert callbacks[-1] == f"cancel:{original.id}"
    assert persisted == []

    backend.preview("builtin.default-light")
    applied = backend.apply()
    assert applied.theme.id == "builtin.default-light"
    assert persisted[-1].selected_theme_id == "builtin.default-light"
    assert persisted[-1].recently_used[0] == "builtin.default-light"
    assert callbacks[-1] == "apply:builtin.default-light"
    assert backend.toggle_favorite("builtin.default-light")
    assert backend.browse(category="favorites").entries[0].theme_id == "builtin.default-light"

    monochrome = backend.inspect("builtin.guc-flag", monochrome=True)
    assert monochrome.monochrome
    for color in monochrome.display_theme.palette.colors():
        value = color[1].lstrip("#")
        assert value[0:2] == value[2:4] == value[4:6]


def test_high_contrast_overlay_preserves_identity_and_readable_semantics() -> None:
    source = ThemeRegistry().get("builtin.default-dark")
    adapted = adapt_theme_high_contrast(source)

    assert adapted.id == source.id
    assert adapted.contrast is ContrastClass.MAXIMUM
    assert adapted.visual_signature != source.visual_signature
    assert adapted.style.selection_style == "reverse"
    assert (
        contrast_ratio(
            adapted.palette.primary_text,
            adapted.palette.app_background,
        )
        >= 7.0
    )
    assert (
        contrast_ratio(
            adapted.palette.selection_foreground,
            adapted.palette.selection_background,
        )
        >= 7.0
    )
    assert len(set(adapted.palette.calendar_event_colors)) == 6


def test_theme_browser_backend_random_cycle_custom_editor_and_repository(
    tmp_path: Path,
) -> None:
    repository = CustomThemeRepository(tmp_path / "custom-themes.json")
    backend = ThemeBrowserBackend(repository)
    random_preview = backend.preview_random(source=random.Random(7))  # noqa: S311
    assert random_preview.theme in backend.registry.themes
    cycled = backend.cycle(1)
    assert cycled.theme == backend.registry.next(random_preview.theme.id)

    custom = backend.duplicate("builtin.default-dark", name="My Complete Theme")
    assert custom.id.startswith("custom.")
    stored = repository.load().themes
    assert len(stored) == 1
    assert stored[0].id == custom.id
    assert stored[0].visual_signature == custom.visual_signature
    assert backend.browse(category="custom").entries[0].theme_id == custom.id
    reopened = ThemeBrowserBackend(repository)
    assert reopened.registry.get(custom.id).visual_signature == custom.visual_signature

    payload = json.loads(backend.export_payload(custom.id))
    payload["name"] = "Edited Complete Theme"
    payload["palette"]["muted_text"] = payload["palette"]["app_background"]
    edited = backend.replace_custom(json.dumps(payload))
    assert edited.name == "Edited Complete Theme"
    issues = backend.inspect(edited.id).issues
    assert any(
        issue.field == "muted_text" and issue.severity is ValidationSeverity.WARNING
        for issue in issues
    )

    exported = tmp_path / "share" / "edited.json"
    backend.export_file(edited.id, exported)
    assert exported.exists()
    backend.preview(edited.id)
    backend.apply()
    assert backend.session.applied.id == edited.id
    removed = backend.delete_custom(edited.id)
    assert removed.id == edited.id
    assert backend.session.applied.id == "builtin.default-dark"
    imported = backend.import_file(exported)
    assert imported.id == edited.id
    backend.rename_custom(imported.id, "Renamed Again")
    assert repository.load().themes[0].name == "Renamed Again"
    backend.reset_custom()
    assert repository.load().themes == ()


def test_branding_browser_backend_search_custom_resize_color_apply_and_cancel() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    persisted: list[BrandingPreferences] = []
    callbacks: list[BrandingPreferences] = []
    backend = BrandingBrowserBackend(
        lambda: theme,
        persist_preferences=persisted.append,
        on_preview=callbacks.append,
        on_apply=callbacks.append,
        on_cancel=callbacks.append,
    )
    snapshot = backend.browse()
    assert len(snapshot.entries) >= 670
    assert len({entry.preset_id for entry in snapshot.entries}) == len(snapshot.entries)
    assert {value for _, value in snapshot.categories} >= {
        "featured-figlet",
        "ansi-figlet",
        "toilet-font",
        "figlet-font",
    }
    assert backend.browse(query="blok").entries[0].name == "GUC Block"
    assert [entry.preset_id for entry in backend.browse(category="custom").entries] == ["custom"]

    original = backend.session.preferences
    preview = backend.preview(
        preset_id="custom",
        visible=True,
        size="full",
        color_behavior="monochrome",
        alignment="center",
        custom_ascii="\x1b[31mGUC\nMAIL",
        target_width=12,
        target_height=3,
        terminal_width=40,
        max_height=6,
        capability=ColorCapability.MONOCHROME,
    )
    assert "\x1b" not in preview.rendered.text
    assert "\x1b" not in preview.preferences.custom_ascii
    assert max(len(line.text.strip()) for line in preview.rendered.lines) <= 12
    assert preview.rendered.height == 3
    assert preview.preferences.alignment is BrandAlignment.CENTER
    assert preview.preferences.color_behavior is BrandColorBehavior.MONOCHROME
    assert backend.session.preferences == original

    cancelled = backend.cancel()
    assert cancelled.preferences == original
    backend.preview(
        preset_id="builtin.guc-mail-standard",
        visible=True,
        size="compact",
        color_behavior="german-flag",
        alignment="right",
        custom_ascii="",
        target_width=None,
        target_height=None,
        terminal_width=60,
        max_height=5,
    )
    applied = backend.apply()
    assert applied.preferences.preset_id == "builtin.guc-mail-standard"
    assert persisted == [applied.preferences]
    assert callbacks[-1] == applied.preferences

    hidden = backend.preview(
        preset_id="none",
        visible=False,
        size="auto",
        color_behavior="theme",
        alignment="left",
        custom_ascii="",
        target_width=None,
        target_height=None,
        terminal_width=40,
        max_height=4,
    )
    assert hidden.rendered.lines == ()

    fallback = backend.preview(
        preset_id="builtin.figlet-fallback-babyfacelame",
        visible=True,
        size="full",
        color_behavior="theme",
        alignment="left",
        custom_ascii="",
        target_width=None,
        target_height=None,
        terminal_width=80,
        max_height=8,
    )
    assert fallback.rendered.fallback_used is True
    assert any("Standard is used explicitly" in warning for warning in fallback.warnings)


@pytest.mark.asyncio
async def test_theme_browser_screen_pilot_search_preview_apply_duplicate_and_editor(
    tmp_path: Path,
) -> None:
    persisted: list[ThemePreferences] = []
    backend = _theme_backend(tmp_path, persisted=persisted)
    screen = ThemeBrowserScreen(backend)
    app = _ScreenHost(screen)

    async with app.run_test(size=(130, 46)) as pilot:
        await pilot.pause()
        table = app.query_one("#theme-table", DataTable)
        assert table.row_count == 661
        app.query_one("#theme-search", Input).value = "defalt drk"
        await pilot.pause()
        assert table.row_count >= 1
        assert screen._selected_theme_id == "builtin.default-dark"

        assert await pilot.click("#theme-preview-action")
        await pilot.pause()
        assert backend.session.is_previewing
        assert "Previewing" in str(app.query_one("#theme-state", Static).content)
        assert await pilot.click("#theme-cancel-preview")
        await pilot.pause()
        assert not backend.session.is_previewing

        app.query_one("#theme-custom-name", Input).value = "Pilot Custom"
        assert await pilot.click("#theme-duplicate")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(backend.registry.custom) == 1
        custom = backend.registry.custom[0]
        assert custom.name == "Pilot Custom"
        stored = backend.repository.load().themes
        assert len(stored) == 1
        assert stored[0].id == custom.id
        assert stored[0].visual_signature == custom.visual_signature

        # The custom editor exposes every palette/style field through validated JSON.
        assert await pilot.click("#theme-edit")
        await pilot.pause()
        assert isinstance(app.screen, ThemeEditorScreen)
        editor_text = app.screen.query_one("#theme-editor-json", TextArea).text
        assert '"command_palette_background"' in editor_text
        assert '"calendar_event_colors"' in editor_text
        assert '"row_density"' in editor_text
        app.screen.query_one("#theme-editor-advanced", Checkbox).value = True
        await pilot.pause()
        editor_payload = json.loads(editor_text)
        editor_payload["name"] = "Pilot Edited"
        app.screen.query_one("#theme-editor-json", TextArea).load_text(json.dumps(editor_payload))
        assert await pilot.click("#theme-editor-preview-action")
        await pilot.pause()
        assert not app.screen.query_one("#theme-editor-save", Button).disabled
        assert await pilot.click("#theme-editor-save")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is screen
        assert backend.registry.get(custom.id).name == "Pilot Edited"
        assert backend.repository.load().themes[0].name == "Pilot Edited"


@pytest.mark.asyncio
async def test_create_custom_theme_action_opens_complete_editor(tmp_path: Path) -> None:
    backend = _theme_backend(tmp_path)
    screen = ThemeBrowserScreen(backend)
    app = _ScreenHost(screen)

    async with app.run_test(size=(130, 46)) as pilot:
        await pilot.pause()
        app.query_one("#theme-custom-name", Input).value = "Fresh Palette Theme"
        screen.action_create_custom_theme()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert isinstance(app.screen, ThemeEditorScreen)
        assert backend.registry.custom[0].name == "Fresh Palette Theme"
        payload = json.loads(app.screen.query_one("#theme-editor-json", TextArea).text)
        assert payload["name"] == "Fresh Palette Theme"
        assert "command_palette_background" in payload["palette"]
        assert "selection_style" in payload["style"]
        assert backend.repository.load().themes[0].name == "Fresh Palette Theme"

        assert await pilot.click("#theme-editor-cancel")
        await pilot.pause()
        assert app.screen is screen


@pytest.mark.asyncio
async def test_branding_browser_screen_pilot_custom_monochrome_apply() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    persisted: list[BrandingPreferences] = []
    backend = BrandingBrowserBackend(
        lambda: theme,
        persist_preferences=persisted.append,
    )
    screen = BrandingBrowserScreen(backend)
    app = _ScreenHost(screen)

    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        table = app.query_one("#branding-table", DataTable)
        assert table.row_count >= 70
        app.query_one("#branding-category", Select).value = "custom"
        await pilot.pause()
        assert table.row_count == 1
        assert screen._selected_preset_id == "custom"
        custom = app.query_one("#branding-custom", TextArea)
        assert not custom.disabled
        custom.load_text("GUC\nMAIL")
        app.query_one("#branding-width", Input).value = "16"
        app.query_one("#branding-height", Input).value = "4"
        app.query_one("#branding-colors", Select).value = "monochrome"
        app.query_one("#branding-visible", Checkbox).value = True

        assert await pilot.click("#branding-preview-action")
        await pilot.pause()
        assert backend.session.is_previewing
        preview_text = str(app.query_one("#branding-preview", Static).content)
        assert "G" in preview_text
        assert await pilot.click("#branding-apply")
        # Applying saves in the background; wait for the save, not for one refresh.
        await wait_until(pilot, lambda: persisted and not backend.session.is_previewing)
        assert not backend.session.is_previewing
        assert persisted[-1].preset_id == "custom"
        assert persisted[-1].target_width == 16
        assert persisted[-1].target_height == 4
        assert persisted[-1].color_behavior is BrandColorBehavior.MONOCHROME


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (79, 26)])
async def test_branding_browser_keeps_primary_actions_visible_at_audit_sizes(
    size: tuple[int, int],
) -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    backend = BrandingBrowserBackend(
        lambda: theme,
        preferences=BrandingPreferences(preset_id="builtin.figlet-standard"),
    )
    screen = BrandingBrowserScreen(backend)
    app = _ScreenHost(screen)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for selector in ("#branding-preview-action", "#branding-apply", "#branding-close"):
            button = app.query_one(selector, Button)
            assert button.display
            assert button.region.y >= 0
            assert button.region.bottom <= size[1]

        screen.action_preview()
        await pilot.pause()
        assert "Select a branding preset" not in str(
            app.query_one("#branding-preview", Static).content
        )


@pytest.mark.parametrize(
    ("name", "first_category", "other_category"),
    [
        ("Forest", "green", "nature"),
        ("Winter", "cool", "seasonal"),
        ("Moonlight", "developer", "low-light"),
        ("Minimal Dark", "core", "minimal"),
        ("Copper", "orange", "luxury"),
        ("Bubblegum", "magenta-pink", "playful"),
    ],
)
def test_shared_theme_is_discoverable_in_both_product_categories(
    tmp_path: Path, name: str, first_category: str, other_category: str
) -> None:
    backend = _theme_backend(tmp_path)
    original = next(t for t in backend.registry.builtins if t.name == name)
    first = backend.browse(query=name, category=first_category).entries
    other = backend.browse(query=name, category=other_category).entries
    assert any(entry.theme_id == original.id for entry in first)
    assert any(entry.theme_id == original.id for entry in other)
    assert sum(t.name == name for t in backend.registry.builtins) == 1
    assert backend.inspect(original.id).theme == original


@pytest.mark.parametrize(
    "name,theme_id",
    [
        ("Professional Blue", "builtin.professional-blue"),
        ("Professional Gray", "builtin.professional-gray"),
    ],
)
def test_professional_category_is_available_and_preserves_academic_discovery(
    tmp_path: Path, name: str, theme_id: str
) -> None:
    backend = _theme_backend(tmp_path)
    assert ("Professional", "professional") in backend.browse().categories
    assert theme_id in {
        entry.theme_id for entry in backend.browse(query=name, category="professional").entries
    }
    assert theme_id in {
        entry.theme_id for entry in backend.browse(query=name, category="academic").entries
    }
    assert backend.registry.get(theme_id).provenance.family == "academic"
    assert sum(theme.id == theme_id for theme in backend.registry.builtins) == 1
