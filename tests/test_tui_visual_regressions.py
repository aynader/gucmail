from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.containers import VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea, Tree

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig, AppPaths, ConfigStore, normalize_theme_identifier
from gucmail.credentials import CredentialResolver, CredentialStore
from gucmail.models import Address, AttachmentInfo, DraftData, MessageSummary
from gucmail.state import StateStore
from gucmail.tui.app import (
    ComposeScreen,
    ConfirmationScreen,
    GUCmailApp,
    HelpScreen,
    PaletteScreen,
    ReaderModeScreen,
)
from gucmail.tui.branding import (
    GERMAN_GOLD,
    GERMAN_RED,
    SLIM_TITLE,
    BrandAlignment,
    BrandColorBehavior,
    BrandingPreferences,
    BrandingSession,
    BrandSize,
    slim_brand,
)
from gucmail.tui.settings_form import SettingRow
from gucmail.tui.settings_screen import SettingsScreen
from gucmail.tui.themes import (
    ThemePreferences,
    ThemeRegistry,
    ThemeSession,
    to_textual_theme,
    validate_theme,
)
from test_tui import _ready_composer_for_editing
from waiting import rounds, wait_until


@pytest.fixture(autouse=True)
def _truecolor_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exact palette assertions require a truecolor terminal. Without COLORTERM, Rich
    # (correctly) quantizes to the xterm-256 palette and the hex values differ.
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("NO_COLOR", raising=False)
    # On Windows Rich ignores COLORTERM and probes the console; Textual reads its
    # override at import time, so pin the already-loaded constant as well.
    import textual.constants

    monkeypatch.setattr(textual.constants, "COLOR_SYSTEM", "truecolor")


def _context(
    tmp_path: Path,
    *,
    theme: str = "builtin.default-dark",
    no_color: bool = False,
) -> AppContext:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        config_file=tmp_path / "config" / "config.toml",
        state_database=tmp_path / "data" / "state.db",
        recovery_dir=tmp_path / "data" / "recovery",
        index_database=tmp_path / "data" / "index.db",
        backup_dir=tmp_path / "data" / "backups",
    )
    config_store = ConfigStore(paths)
    config = AppConfig(
        email="visual-audit@example.test",
        theme=theme,
        no_color=no_color,
    )
    config_store.save(config)
    return AppContext(
        paths=paths,
        config_store=config_store,
        config=config,
        state=StateStore(paths.state_database),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def _messages(count: int = 24) -> tuple[MessageSummary, ...]:
    return tuple(
        MessageSummary(
            reference=f"synthetic-{index}",
            subject=f"Synthetic visual row {index:02d}",
            sender=Address("sender@example.test", "Synthetic Sender"),
            received_at=datetime(2026, 8, 9, 9, index % 60, tzinfo=UTC),
            is_read=index % 2 == 0,
            size=1_024 + index,
        )
        for index in range(count)
    )


def _assert_inside_screen(widget: object, *, width: int, height: int) -> None:
    region = widget.region  # type: ignore[attr-defined]
    assert region.x >= 0
    assert region.y >= 0
    assert region.right <= width
    assert region.bottom <= height


def test_theme_and_branding_preview_sessions_persist_only_on_apply() -> None:
    registry = ThemeRegistry()
    assert len(registry.builtins) == 661
    assert len({theme.visual_signature for theme in registry.builtins}) == 661
    assert all(not validate_theme(theme) for theme in registry.builtins)

    persisted_themes: list[ThemePreferences] = []
    themes = ThemeSession(registry, persist=persisted_themes.append)
    original_theme = themes.applied
    preview_theme = registry.next(original_theme.id)
    assert themes.preview(preview_theme.id) == preview_theme
    assert themes.is_previewing
    assert themes.cancel() == original_theme
    assert not persisted_themes

    themes.preview(preview_theme.id)
    assert themes.apply() == preview_theme
    assert persisted_themes == [themes.preferences]
    assert ThemeSession(registry, persisted_themes[-1]).applied == preview_theme

    persisted_branding: list[BrandingPreferences] = []
    branding = BrandingSession(persist=persisted_branding.append)
    original_branding = branding.current
    branding.preview(
        alignment=BrandAlignment.CENTER,
        color_behavior=BrandColorBehavior.GERMAN_FLAG,
    )
    assert branding.cancel() == original_branding
    assert not persisted_branding
    branding.preview(alignment=BrandAlignment.RIGHT)
    applied_branding = branding.apply()
    assert persisted_branding == [applied_branding]


def test_branding_preferences_reload_across_app_reconstruction(tmp_path: Path) -> None:
    context = _context(tmp_path)
    first = GUCmailApp(context, offline=True)
    first.branding_browser_backend.session.preview(
        preset_id="custom",
        visible=True,
        size=BrandSize.COMPACT,
        color_behavior=BrandColorBehavior.MONOCHROME,
        alignment=BrandAlignment.RIGHT,
        custom_ascii="GUC\nMAIL",
        target_width=19,
        target_height=4,
    )
    applied = first.branding_browser_backend.apply().preferences

    reloaded_context = AppContext(
        paths=context.paths,
        config_store=context.config_store,
        config=context.config_store.load(),
        state=context.state,
        credential_resolver=context.credential_resolver,
    )
    second = GUCmailApp(reloaded_context, offline=True)

    assert second.branding_preferences == applied
    assert second.branding_preferences == BrandingPreferences(
        preset_id="custom",
        visible=True,
        size=BrandSize.COMPACT,
        color_behavior=BrandColorBehavior.MONOCHROME,
        alignment=BrandAlignment.RIGHT,
        custom_ascii="GUC\nMAIL",
        target_width=19,
        target_height=4,
    )


def test_theme_session_rolls_back_when_persistence_fails() -> None:
    registry = ThemeRegistry()

    def fail_to_persist(_preferences: ThemePreferences) -> None:
        raise OSError("synthetic persistence failure")

    session = ThemeSession(registry, persist=fail_to_persist)
    original = session.applied
    preview = registry.next(original.id)
    session.preview(preview.id)

    with pytest.raises(OSError, match="synthetic persistence failure"):
        session.apply()

    assert session.applied == original
    assert session.current == preview
    assert session.is_previewing


def test_semantic_theme_maps_completely_to_textual_variables() -> None:
    definition = ThemeRegistry().get("builtin.default-dark")
    converted = to_textual_theme(definition)
    colors = converted.to_color_system().generate()

    assert converted.name == "gucmail-builtin-default-dark"
    assert converted.dark
    assert colors["background"].casefold() == definition.palette.app_background.casefold()
    assert colors["text-muted"].casefold() == definition.palette.muted_text.casefold()
    assert (
        colors["block-cursor-background"].casefold()
        == definition.palette.selection_background.casefold()
    )
    assert colors["scrollbar"].casefold() == definition.palette.scrollbar_thumb.casefold()
    assert colors["footer-background"].casefold() == definition.palette.footer.casefold()
    assert converted.variables["gucmail-header"] == definition.palette.header
    assert converted.variables["gucmail-logo-accent"] == definition.palette.logo_accent


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("light", "builtin.default-light"),
        ("GUCMAIL-BUILTIN-DEFAULT-LIGHT", "builtin.default-light"),
        ("builtin.default_light", "builtin.default-light"),
        ("monochrome", "builtin.monochrome-dark"),
    ],
)
def test_config_theme_identifiers_normalize_to_stable_catalog_ids(
    configured: str,
    expected: str,
) -> None:
    assert normalize_theme_identifier(configured) == expected


@pytest.mark.asyncio
async def test_runtime_theme_mount_resolves_registers_and_applies_config_selection(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path, theme="GUCMAIL-BUILTIN-DEFAULT-LIGHT")
    app = GUCmailApp(context, offline=True)

    assert app.active_theme.id == "builtin.default-light"
    assert context.config.theme == "builtin.default-light"
    assert app.theme == "gucmail-builtin-default-light"
    assert app.theme in app.available_themes

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        palette = app.active_theme.palette
        assert app.current_theme.name == app.theme
        assert not app.current_theme.dark
        assert app.screen.styles.background.hex.casefold() == palette.app_background
        assert (
            app.query_one("#navigation").styles.background.hex.casefold()
            == palette.sidebar_background
        )
        assert (
            app.query_one("#messages").styles.background.hex.casefold()
            == palette.message_list_background
        )
        assert (
            app.query_one("#preview-scroll").styles.background.hex.casefold()
            == palette.reading_pane_background
        )


@pytest.mark.asyncio
async def test_every_theme_family_applies_to_rendered_shell_palette_and_modal(
    tmp_path: Path,
) -> None:
    """Render one distinct runtime sample from every generated visual family."""

    app = GUCmailApp(_context(tmp_path), offline=True)
    representatives = {}
    for theme in app.theme_registry.builtins:
        representatives.setdefault(theme.provenance.family, theme)
    assert len(representatives) == 29

    rendered_signatures: set[tuple[str, ...]] = set()
    structural_signatures: set[tuple[object, ...]] = set()
    async with app.run_test(size=(140, 40)) as pilot:
        app._messages = _messages()
        for family, definition in sorted(representatives.items()):
            app.preview_theme(definition.id)
            app._render_message_rows(app._messages)
            await pilot.pause()
            palette = definition.palette
            surfaces = (
                app.screen.styles.background.hex.casefold(),
                app.query_one("#navigation").styles.background.hex.casefold(),
                app.query_one("#messages").styles.background.hex.casefold(),
                app.query_one("#preview-scroll").styles.background.hex.casefold(),
                app.query_one("CompactHeader").styles.background.hex.casefold(),
                app.query_one("#status-line").styles.background.hex.casefold(),
            )
            assert surfaces == (
                palette.app_background,
                palette.sidebar_background,
                palette.message_list_background,
                palette.reading_pane_background,
                palette.header,
                palette.footer,
            ), family

            await pilot.press("ctrl+k")
            assert isinstance(app.screen, PaletteScreen)
            assert (
                app.screen.query_one("#palette-box").styles.background.hex.casefold()
                == palette.command_palette_background
            ), family
            await pilot.press("escape")

            app.push_screen(ReaderModeScreen())
            await pilot.pause()
            assert (
                app.screen.query_one("#reader-mode-box").styles.background.hex.casefold()
                == palette.modal_background
            ), family
            app.screen.dismiss(None)
            await pilot.pause()

            rendered_signatures.add(
                (*surfaces, palette.command_palette_background, palette.modal_background)
            )
            table = app.query_one("#message-table", DataTable)
            cursor = table.get_component_styles("datatable--cursor")
            structural_signatures.add(
                (
                    table.cell_padding,
                    app.query_one("#messages").styles.border.top[0],
                    cursor.background.hex,
                    str(cursor.text_style),
                    app.query_one("#expanded-brand").styles.border_bottom[0],
                )
            )

    assert len(rendered_signatures) == len(representatives)
    assert len(structural_signatures) >= 10


@pytest.mark.asyncio
async def test_runtime_theme_unknown_selection_falls_back_without_breaking_mount(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(_context(tmp_path, theme="custom.missing"), offline=True)

    assert app.theme_fallback_used
    assert app.active_theme.id == "builtin.default-dark"
    assert app.context.config.theme == "builtin.default-dark"
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.current_theme.name == "gucmail-builtin-default-dark"
        assert app.current_theme.dark


@pytest.mark.asyncio
async def test_runtime_theme_honors_no_color_for_palette_and_slim_brand(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(
        _context(tmp_path, theme="builtin.default-light", no_color=True),
        offline=True,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        colors = app.current_theme.to_color_system().generate()
        assert colors["background"].casefold() == "#ffffff"
        assert colors["foreground"].casefold() == "#000000"
        brand_colors = tuple(
            app.query_one(selector, Static).styles.color.hex
            for selector in ("#brand-g", "#brand-u", "#brand-c")
        )
        assert brand_colors == ("#000000", "#000000", "#000000")


@pytest.mark.asyncio
async def test_runtime_theme_preview_cancel_apply_and_same_id_refresh(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)

    async with app.run_test(size=(100, 30)) as pilot:
        original = app.active_theme
        preview = app.preview_theme("light")
        await pilot.pause()
        assert preview.id == "builtin.default-light"
        assert app.active_theme == preview
        assert context.config.theme == original.id
        assert context.config_store.load().theme == original.id

        assert app.cancel_theme_preview() == original
        await pilot.pause()
        assert app.active_theme == original
        assert not app.theme_session.is_previewing

        app.preview_theme(preview.id)
        applied = app.apply_theme()
        await pilot.pause()
        assert applied == preview
        assert context.config.theme == preview.id
        assert context.config_store.load().theme == preview.id
        assert not app.theme_session.is_previewing

        custom = app.theme_registry.duplicate(
            preview.id,
            name="Runtime refresh audit",
            custom_id="custom.runtime-refresh-audit",
        )
        app.preview_theme(custom.id)
        await pilot.pause()
        app.theme_registry.edit_custom(
            custom.id,
            palette_overrides={"app_background": "#fff8f0"},
        )
        app.preview_theme(custom.id)
        await pilot.pause()
        assert app.screen.styles.background.hex.casefold() == "#fff8f0"


@pytest.mark.asyncio
async def test_settings_dropdown_stages_and_persists_choice(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_area("settings")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await wait_until(pilot, lambda: screen._catalog is not None, interval=0.05)
        assert screen._catalog is not None, str(screen.query_one("#area-state", Static).render())
        assert screen.action_select_setting("mail.mark_read_on_open")
        await pilot.pause()
        choice = screen.query_one("#settings-choice", Select)
        assert choice.display
        choice.value = "off"
        screen._validate_editor(preview=False)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen._staged["mail.mark_read_on_open"] is False
        screen.action_apply()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._settings_service().value("mail.mark_read_on_open").effective_value is False
        assert screen.action_select_setting("appearance.theme")
        await pilot.pause()
        assert choice.display, (
            screen._selected_setting_id,
            screen.query_one("#settings-search", Input).value,
            screen.query_one("#settings-level", Select).value,
        )
        assert not screen.query_one("#settings-managed", Button).disabled
        assert screen.query_one("#settings-editor", Input).display is False


@pytest.mark.asyncio
async def test_small_composer_scrolls_controls_and_keeps_palette_available(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        app.action_compose()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ComposeScreen)
        screen.query_one("#compose-to", Input).focus()
        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        await pilot.press("escape")
        assert app.screen is screen
        body = screen.query_one("#compose-body", TextArea)
        for _ in range(18):
            if body.has_focus:
                break
            await pilot.press("tab")
        await pilot.pause()
        assert body.has_focus
        viewport = screen.query_one("#compose-fields", VerticalScroll).content_region
        assert viewport.contains_region(body.region)
        await pilot.press("tab", "tab", "tab", "tab", "tab")
        await pilot.pause()
        send = screen.query_one("#compose-send", Button)
        assert send.has_focus
        assert screen.query_one("#composer").content_region.contains_region(send.region)
        for selector in ("#compose-save", "#compose-send"):
            button = screen.query_one(selector, Button)
            assert button.content_size.width >= cell_len(str(button.label))


@pytest.mark.parametrize("width", [80, 120, 140])
def test_slim_branding_is_exact_and_semantically_tricolored(width: int) -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    rendered = slim_brand(theme)

    assert rendered.text == SLIM_TITLE == "GUC Mail CLI"
    assert rendered.width <= width
    spans = rendered.lines[0].spans
    assert tuple(span.text for span in spans[:3]) == ("G", "U", "C")
    assert tuple(span.foreground for span in spans[:3]) == (
        "#f5f5f5",
        GERMAN_RED,
        GERMAN_GOLD,
    )
    assert spans[3].text == " Mail CLI"
    assert all(span.background is None for span in spans)


@pytest.mark.asyncio
async def test_main_tui_branding_table_and_responsive_geometry(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(80, 32)) as pilot:
        app._messages = _messages()
        app._render_message_rows(app._messages)
        await pilot.pause()

        title_parts = (
            app.query_one("#brand-g", Static),
            app.query_one("#brand-u", Static),
            app.query_one("#brand-c", Static),
            app.query_one("#brand-name", Static),
        )
        assert tuple(str(part.content) for part in title_parts) == ("G", "U", "C", " Mail CLI")
        expanded_brand = app.query_one("#expanded-brand", Static)
        assert not expanded_brand.display
        assert app.query_one("#compact-title").display

        app.branding_browser_backend.session.preview(visible=False)
        app.branding_browser_backend.apply()
        await pilot.pause()
        assert not expanded_brand.display
        app.branding_browser_backend.session.preview(visible=True)
        app.branding_browser_backend.apply()
        await pilot.pause()
        assert not expanded_brand.display
        assert app.query_one("#compact-title").display

        # Explicitly selected multi-line art remains available as a separate banner.
        app.branding_browser_backend.session.preview(
            preset_id="builtin.guc-logo-image-sampled", size=BrandSize.FULL
        )
        app.branding_browser_backend.apply()
        await pilot.pause()
        assert expanded_brand.display
        # The main shell has one product mark and no isolated background patch.
        assert title_parts[0].styles.background.a == 0
        assert not app.query_one("#compact-title").display
        assert title_parts[1].styles.color.hex == GERMAN_RED.upper()
        gold = title_parts[2].styles.color.rgb
        assert gold[0] == 255 and gold[1] >= 204 and gold[2] == 0

        header = app.query_one("CompactHeader")
        assert header.region.height == 1
        # display changes synchronously, but Textual's hit map is rebuilt on refresh.
        # Wait for the restored banner to be laid out before calculating click coordinates.
        for _ in rounds(30):
            region = expanded_brand.region
            if region.height and app.get_widget_at(region.x, region.y)[0] is expanded_brand:
                break
            await pilot.pause(0.025)
        assert expanded_brand.region.height > 0
        assert app.get_widget_at(*expanded_brand.region.offset)[0] is expanded_brand
        before_click = header.region
        assert await pilot.click("#expanded-brand")
        await pilot.pause()
        assert header.region == before_click
        assert header.region.height == 1

        table = app.query_one("#message-table", DataTable)
        messages = app.query_one("#messages")
        message_filter = app.query_one("#message-filter", Input)
        assert table.row_count == 24
        assert table.region.height == (
            messages.content_region.height
            - message_filter.region.height
            - app.query_one("#message-heading").region.height
        )
        assert table.region.height > 3

        assert app.has_class("medium")
        assert app.query_one("#navigation").display
        assert not app.query_one("#pane-divider").display
        assert not app.query_one("#preview-scroll").display

        for width in (120, 140):
            await pilot.resize_terminal(width, 36)
            await pilot.pause()
            assert app.has_class("wide")
            assert header.region == header.region.__class__(0, 0, width, 1)
            divider = app.query_one("#pane-divider")
            preview = app.query_one("#preview-scroll", VerticalScroll)
            assert divider.display and divider.region.width == 1
            assert preview.display and preview.region.width > 0
            assert table.region.height > 3
            for part in title_parts:
                _assert_inside_screen(part, width=width, height=36)

        await pilot.resize_terminal(79, 30)
        await pilot.pause()
        assert app.has_class("narrow")
        assert not app.query_one("#navigation").display
        assert not app.query_one("#pane-divider").display
        assert not app.query_one("#preview-scroll").display
        await pilot.press("ctrl+b")
        await pilot.pause()
        assert app.query_one("#navigation").display
        _assert_inside_screen(app.query_one("#navigation"), width=79, height=30)


@pytest.mark.asyncio
async def test_tui_focus_scroll_and_modal_readability(tmp_path: Path) -> None:
    width, height = 120, 36
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(width, height)) as pilot:
        app._messages = _messages()
        app._render_message_rows(app._messages)
        await pilot.pause()

        assert isinstance(app.focused, DataTable)
        await pilot.press("shift+tab", "shift+tab")
        assert isinstance(app.focused, Tree)
        await pilot.press("tab")
        assert isinstance(app.focused, Input)
        await pilot.press("tab")
        assert isinstance(app.focused, DataTable)
        await pilot.press("tab")
        assert isinstance(app.focused, VerticalScroll)

        preview = app.query_one("#preview-scroll", VerticalScroll)
        app.query_one("#preview", Static).update("\n".join(f"Line {index}" for index in range(200)))
        preview.focus()
        await wait_until(pilot, lambda: preview.max_scroll_y > 0, interval=0.025, pause_first=True)
        assert preview.max_scroll_y > 0
        await pilot.press("end")
        await wait_until(pilot, lambda: preview.scroll_y > 0, interval=0.025, pause_first=True)
        assert preview.scroll_y > 0

        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        _assert_inside_screen(
            app.screen.query_one("#help-article-scroll"), width=width, height=height
        )
        await pilot.press("escape")

        await pilot.press("ctrl+k")
        assert isinstance(app.screen, PaletteScreen)
        _assert_inside_screen(app.screen.query_one("#palette-box"), width=width, height=height)
        assert isinstance(app.screen.focused, Input)
        await pilot.press("tab")
        assert app.screen.focused is not None
        await pilot.press("escape")
        assert app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_required_terminal_size_matrix_has_no_clipped_shell_or_palette(
    tmp_path: Path,
) -> None:
    """Exercise every exact geometry required by the second-pass visual gate."""

    sizes = (
        (72, 24),
        (90, 26),
        (100, 30),
        (120, 35),
        (140, 40),
        (160, 45),
        (192, 50),
    )
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=sizes[0]) as pilot:
        app._messages = _messages()
        app._render_message_rows(app._messages)
        for width, height in sizes:
            await pilot.resize_terminal(width, height)
            await pilot.pause()

            header = app.query_one("CompactHeader")
            mail_layout = app.query_one("#mail-layout")
            status = app.query_one("#status-bar", Static)
            table = app.query_one("#message-table", DataTable)
            for widget in (header, mail_layout, status, table):
                _assert_inside_screen(widget, width=width, height=height)
            assert header.region == header.region.__class__(0, 0, width, 1)
            assert header.region.bottom <= mail_layout.region.y
            assert mail_layout.region.bottom <= status.region.y
            assert table.region.height >= 3

            if width >= 120:
                assert app.has_class("wide")
                assert app.query_one("#preview-scroll").display
            elif width >= 80:
                assert app.has_class("medium")
                assert not app.query_one("#preview-scroll").display
            else:
                assert app.has_class("narrow")
                assert not app.query_one("#preview-scroll").display

            await pilot.press("ctrl+k")
            assert isinstance(app.screen, PaletteScreen)
            palette_box = app.screen.query_one("#palette-box")
            _assert_inside_screen(palette_box, width=width, height=height)
            assert palette_box.region.y == 2
            assert abs(palette_box.region.x - ((width - palette_box.region.width) // 2)) <= 1
            await pilot.press("escape")


@pytest.mark.asyncio
async def test_forward_review_discloses_inherited_and_local_attachments(tmp_path: Path) -> None:
    attachment = tmp_path / "added.txt"
    attachment.write_text("Synthetic attachment", encoding="utf-8")
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        draft = DraftData(
            reference="synthetic-forward",
            subject="[test] literal subject",
            forward_of_reference="synthetic-source",
            attachment_paths=[attachment],
            to=[Address("recipient@example.test")],
            body_text="Please see the forwarded message.",
        )
        app._open_compose(
            draft,
            inherited_attachments=(
                AttachmentInfo(reference="synthetic-attachment", name="[source].txt", size=25),
            ),
        )
        await pilot.pause()
        composer = app.screen
        assert isinstance(composer, ComposeScreen)
        assert "[source].txt" in str(composer.query_one("#compose-inherited", Static).render())
        async with asyncio.timeout(15):
            while composer._attempt_pending:
                await asyncio.sleep(0.005)
        composer.action_review_send()
        async with asyncio.timeout(15):
            while not isinstance(app.screen, ConfirmationScreen):
                await asyncio.sleep(0.005)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "[source].txt" in app.screen.detail
        assert "added.txt" in app.screen.detail
        assert "[test] literal subject" in app.screen.detail
        for widget in app.screen.query(Static):
            assert not widget._render_markup


@pytest.mark.asyncio
async def test_unresolved_forward_attachments_block_send_review(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_compose(
            DraftData(reference="synthetic-forward", forward_of_reference="synthetic-source")
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        assert isinstance(composer, ComposeScreen)
        assert composer.query_one("#compose-send", Button).disabled
        composer.action_review_send()
        assert app.screen is composer


@pytest.mark.asyncio
async def test_sender_width_leaves_subject_visible_and_reflows(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._render_message_rows(_messages())
        await pilot.pause()
        table = app.query_one("#message-table", DataTable)
        widths = [column.width for column in table.columns.values()]
        assert widths[2] >= 20
        assert sum(widths[:3]) + 6 <= table.content_size.width
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        widths = [column.width for column in table.columns.values()]
        assert sum(widths[:3]) + 6 <= table.content_size.width


@pytest.mark.asyncio
async def test_draft_save_missing_attachment_retains_form_and_explains_error(
    tmp_path: Path,
) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._open_compose(
            DraftData(
                reference="dft-missing",
                subject="Retained",
                attachment_paths=[tmp_path / "missing.txt"],
            )
        )
        await pilot.pause()
        composer = await _ready_composer_for_editing(pilot)
        assert isinstance(composer, ComposeScreen)
        assert await pilot.click("#compose-save")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is composer
        assert composer.query_one("#compose-subject", Input).value == "Retained"
        note = str(composer.query_one("#compose-note", Static).render())
        assert "missing.txt" in note and "unavailable" in note.lower()
        assert not composer.query_one("#compose-save", Button).disabled


@pytest.mark.asyncio
async def test_task_repeat_controls_build_weekly_rule(tmp_path: Path) -> None:
    from gucmail.tui.pim_screens import TaskEditorScreen

    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        screen = TaskEditorScreen(None)
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#task-edit-subject", Input).value = "Synthetic repeat"
        screen.query_one("#task-edit-start", Input).value = "2026-09-20"
        screen.query_one("#task-edit-repeat", Select).value = "weekly"
        await pilot.pause()
        assert screen.query_one("#task-repeat-options").display
        assert not screen.query_one("#task-edit-recurrence").display
        screen.query_one("#task-repeat-count", Input).value = "4"
        recurrence = screen._value().recurrence
        assert recurrence is not None
        assert recurrence["pattern"]["type"] == "weekly"
        assert recurrence["pattern"]["weekdays"] == ["sunday"]
        assert recurrence["boundary"]["occurrences"] == 4


@pytest.mark.asyncio
async def test_folder_switch_clears_preview_and_invalidates_pending_load(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app.query_one("#preview", Static).update("Previous folder message")
        app._message_detail_reference = "old"
        generation = app._message_generation
        app._activate_folder_location("deleted")
        await pilot.pause()
        assert app._message_generation > generation
        assert app._message_detail_reference is None
        assert "Previous folder message" not in str(app.query_one("#preview", Static).render())


@pytest.mark.asyncio
async def test_guided_theme_editor_roundtrips_palette_and_style(tmp_path: Path) -> None:
    from dataclasses import replace

    from textual.widgets import Checkbox

    from gucmail.tui.appearance_screen import ThemeEditorScreen
    from gucmail.tui.themes import export_theme

    app = GUCmailApp(_context(tmp_path), offline=True)
    theme = replace(
        ThemeRegistry().get("builtin.default-dark"), id="custom.regression", name="Original"
    )
    async with app.run_test(size=(120, 40)) as pilot:
        screen = ThemeEditorScreen(export_theme(theme))
        app.push_screen(screen)
        await pilot.pause()
        screen.query_one("#theme-editor-name", Input).value = "Edited visually"
        screen.query_one("#theme-editor-color-value", Input).value = "#101010"
        screen.query_one("#theme-editor-bold", Checkbox).value = False
        screen.query_one("#theme-editor-density", Select).value = "spacious"
        edited = screen._validate(update_preview=True)
        assert edited is not None
        assert edited.name == "Edited visually"
        assert edited.palette.app_background == "#101010"
        assert edited.style.row_density == "spacious" and not edited.style.bold_text
        screen.query_one("#theme-editor-color-field", Select).value = "primary_accent"
        await pilot.pause()
        assert (
            screen.query_one("#theme-editor-color-value", Input).value
            == theme.palette.primary_accent
        )
        screen.query_one("#theme-editor-advanced", Checkbox).value = True
        await pilot.pause()
        assert '"#101010"' in screen.query_one("#theme-editor-json", TextArea).text
        screen.query_one("#theme-editor-json", TextArea).text = "invalid"
        screen.query_one("#theme-editor-advanced", Checkbox).value = False
        await pilot.pause()
        assert screen.query_one("#theme-editor-advanced", Checkbox).value
        assert "Correct the JSON" in str(screen.query_one("#theme-editor-state", Static).render())


def test_resumed_forward_preserves_compose_kind() -> None:
    from gucmail.services.compose import ComposeKind

    screen = ComposeScreen(DraftData(reference="forward", forward_of_reference="source"))
    assert screen.kind is ComposeKind.FORWARD
    assert screen.inherited_attachments is None


@pytest.mark.asyncio
async def test_draft_manager_recovers_newest_snapshot(tmp_path: Path) -> None:
    import os

    from gucmail.services.drafts import DraftService
    from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen

    context = _context(tmp_path)
    service = DraftService(context.state, context.paths.recovery_dir)
    older = service.write_recovery(DraftData(reference="zzz-old", subject="Older"))
    newer = service.write_recovery(DraftData(reference="aaa-new", subject="Newest"))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_010, 1_700_000_010))
    assert service.recovery_references() == ["zzz-old", "aaa-new"]
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._open_area("drafts")
        await pilot.pause()
        assert isinstance(app.screen, DraftAttachmentScreen)
        assert await pilot.click("#draft-recover")
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one("#compose-subject", Input).value == "Newest"


@pytest.mark.asyncio
async def test_activity_formats_utc_and_keeps_ids_in_details(tmp_path: Path) -> None:
    from gucmail.tui.activity_screen import ActivityEventView, ActivityScreen

    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._open_area("activity")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        assert screen._time("2026-09-17T06:00:00+00:00") == "2026-09-17 09:00 EEST"
        assert screen._time("2026-09-17 06:00:00") == screen._time("2026-09-17T06:00:00Z")
        screen._render_events(
            (
                ActivityEventView(
                    "opaque-event-id",
                    "2026-09-17T06:00:00Z",
                    "mail",
                    "info",
                    "Saved draft",
                    "Detail",
                ),
            )
        )
        table = screen.query_one("#activity-table", DataTable)
        assert len(table.columns) == 4
        assert "opaque-event-id" not in str(table.get_row("opaque-event-id"))
        assert "opaque-event-id" in str(screen.query_one("#activity-detail", Static).render())


@pytest.mark.asyncio
async def test_composer_optional_addresses_and_selected_attachment_removal(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = ComposeScreen(DraftData(reference="cc", cc=[Address("synthetic@example.test")]))
        app.push_screen(screen)
        await pilot.pause()
        await _ready_composer_for_editing(pilot)
        assert screen.query_one("#compose-cc-row").display
        assert await pilot.click("#compose-extra-addresses")
        assert not screen.query_one("#compose-cc-row").display
        assert screen._payload(action="save")["cc"] == "synthetic@example.test"
        screen.add_attachment_path(tmp_path / "first.txt")
        screen.add_attachment_path(tmp_path / "second.txt")
        await pilot.pause()
        screen.query_one("#compose-attachment-list", Select).value = str(tmp_path / "first.txt")
        assert await pilot.click("#compose-attachment-remove")
        await pilot.pause()
        assert screen._payload(action="save")["attachment_paths"] == str(tmp_path / "second.txt")


@pytest.mark.asyncio
async def test_contact_repeatable_fields_preserve_values_and_validate_duplicates(
    tmp_path: Path,
) -> None:
    from gucmail.models import ContactInfo
    from gucmail.tui.contact_fields import ContactValueRow, ContactValuesEditor
    from gucmail.tui.pim_screens import ContactEditorScreen

    contact = ContactInfo(
        reference="synthetic",
        display_name="Synthetic",
        phones={"MobilePhone": "00000"},
        addresses={"Home": "Street; building A\nExample town"},
    )
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(120, 45)) as pilot:
        screen = ContactEditorScreen(contact)
        app.push_screen(screen)
        await pilot.pause()
        assert screen._value().phones == contact.phones
        assert screen._value().addresses == contact.addresses
        phones = screen.query_one("#contact-edit-phones", ContactValuesEditor)
        add = phones.query_one(".contact-value-add", Button)
        add.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert await pilot.click(add)
        await pilot.pause()
        rows = list(phones.query(ContactValueRow))
        assert len(rows) == 2
        rows[1].query_one(Input).value = "11111"
        assert screen._value().phones == {"MobilePhone": "00000", "BusinessPhone": "11111"}
        rows[1].query_one(Select).value = "MobilePhone"
        screen.action_save()
        assert app.screen is screen
        assert "different type" in str(screen.query_one("#contact-editor-error", Static).render())
        rows[1].query_one(".contact-value-remove", Button).press()
        await wait_until(
            pilot,
            lambda: len(phones.query(ContactValueRow)) == 1,
            interval=0.025,
            pause_first=True,
        )
        assert len(phones.query(ContactValueRow)) == 1
        assert screen._value().phones == contact.phones


@pytest.mark.asyncio
async def test_restore_palette_has_one_exact_confirmation(tmp_path: Path, monkeypatch) -> None:
    from gucmail.tui.app import PaletteParameterReviewScreen

    app = GUCmailApp(_context(tmp_path), offline=True)
    executed = []
    monkeypatch.setattr(app, "_execute_message_mutation", lambda plan, **kw: executed.append(plan))
    async with app.run_test(size=(140, 45)) as pilot:
        await wait_until(pilot, lambda: not app.ui.busy, interval=0.03, pause_first=True)
        # Only the presentation context is connected. The preview does not access EWS.
        app.offline = False
        app.ui.online = True
        app._render_messages([
            MessageSummary("synthetic-restore", "Synthetic selection", None, None)
        ])
        app.ui.selected_reference = "synthetic-restore"
        app.action_palette()
        await pilot.pause()
        app.screen.query_one("#palette-input", Input).value = "restore this"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen), str(
            app.screen.query_one("#palette-explanation", Static).content
        )
        assert not any(isinstance(s, PaletteParameterReviewScreen) for s in app.screen_stack)
        assert len([s for s in app.screen_stack if isinstance(s, ConfirmationScreen)]) == 1
        assert not executed
        # A later selection must not change the exact target already reviewed.
        app.ui.selected_reference = "different-item"
        await pilot.press("y")
        await pilot.pause()
        assert len(executed) == 1
        assert executed[0].targets == ("synthetic-restore",)
        assert dict(executed[0].arguments)["destination"] == "inbox"


@pytest.mark.asyncio
async def test_settings_numeric_range_and_advanced_disclosure(tmp_path: Path) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(100, 35)) as pilot:
        app._open_area("settings")
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await wait_until(
            pilot, lambda: screen._catalog is not None, interval=0.05, pause_first=True
        )
        assert screen._catalog is not None
        setting = next(s for s in screen._catalog.settings if s.setting_id.endswith("page_size"))
        assert screen.action_select_setting(setting.setting_id)
        await pilot.pause()
        editor = screen.query_one("#settings-editor", Input)
        assert editor.type == "integer"
        details = str(screen.query_one("#settings-detail", Static).render())
        assert "Range:" in details and "Scope:" not in details
        editor.value = str(int(setting.maximum or 500) + 1)
        typed_value = editor.value
        row = next(row for row in screen.query(SettingRow) if row.setting_id == setting.setting_id)
        screen._form_row_selected(SettingRow.Selected(row))
        assert editor.value == typed_value
        screen._validate_editor(preview=False)
        await pilot.pause()
        assert not screen._staged
        assert editor.validate(editor.value).is_valid is False
        editor.value = "25"
        screen._validate_editor(preview=False)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen._staged[setting.setting_id] == 25
        screen.query_one("#settings-level", Select).value = "advanced"
        await pilot.pause()
        screen.action_select_setting(setting.setting_id)
        assert "Scope:" in str(screen.query_one("#settings-detail", Static).render())
        screen.action_select_setting("account.email")
        assert editor.type == "text" and not editor.validators


@pytest.mark.asyncio
async def test_notification_provider_privacy_controls_fit_small_screen(tmp_path: Path) -> None:
    from textual.widgets import Checkbox

    from gucmail.tui.notification_settings_screen import ProviderEditorScreen
    from test_compose_preferences import wait_for

    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = ProviderEditorScreen(None, None, digest_ids=())
        app.push_screen(screen)
        await pilot.pause()
        flags = screen.query_one("#notification-provider-flags")
        for checkbox in flags.query(Checkbox):
            checkbox.focus()
            # DescendantFocus scrolls the editor on the next layout refresh.
            # CPU-idle pause alone can return before that refresh under load.
            await wait_for(
                pilot,
                lambda checkbox=checkbox: (
                    checkbox.has_focus and checkbox.region.y >= 0 and checkbox.region.bottom <= 24
                ),
            )
            assert checkbox.region.x >= flags.region.x
            assert checkbox.region.right <= flags.region.right
            assert checkbox.region.width >= len(str(checkbox.label)) + 8
            _assert_inside_screen(checkbox, width=80, height=24)
        save = screen.query_one("#notification-provider-save", Button)
        save.focus()
        await pilot.pause()
        _assert_inside_screen(save, width=80, height=24)
        await pilot.press("escape")
        assert app.screen is not screen


@pytest.mark.asyncio
async def test_header_resize_keeps_compact_send_limit(tmp_path: Path) -> None:
    from rich.cells import cell_len

    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._render_status(
            {"exchange_version": "Synthetic Exchange version", "inbox_count": 3},
            quota_label="Mailbox limit 92 MiB",
        )
        await pilot.pause()
        bar = app.query_one("#account-bar", Static)
        assert "limit 92 MiB" in str(bar.content)
        assert "Synthetic Exchange version" in str(bar.tooltip)
        for width in (80, 65, 140):
            await pilot.resize_terminal(width, 24)
            await pilot.pause()
            assert cell_len(str(bar.content)) <= bar.content_size.width
            assert "Connected" in str(bar.content)
            assert "limit 92 MiB" in str(bar.content)


@pytest.mark.asyncio
async def test_composer_keeps_semicolon_filename_through_review_and_reopen(tmp_path: Path) -> None:
    attachment = tmp_path / "synthetic;attachment.txt"
    attachment.write_text("Synthetic attachment", encoding="utf-8")
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        screen = ComposeScreen(
            DraftData(
                reference="synthetic-semicolon",
                to=[Address("recipient@example.test")],
                subject="Synthetic attachment",
                body_text="Here is the requested file.",
            )
        )
        app.push_screen(screen)
        await pilot.pause()
        async with asyncio.timeout(15):
            while screen._attempt_pending:
                await asyncio.sleep(0.005)
        screen.add_attachment_path(attachment)
        await pilot.pause()
        payload = screen._payload(action="save")
        draft = app._draft_from_payload(payload)
        assert draft.attachment_paths == [attachment]
        screen.action_review_send()
        async with asyncio.timeout(15):
            while not isinstance(app.screen, ConfirmationScreen):
                await asyncio.sleep(0.005)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        assert "synthetic;attachment.txt" in app.screen.detail
        assert "Local attachments (1)" in app.screen.detail
        await pilot.press("escape")
        app.push_screen(ComposeScreen(draft))
        await pilot.pause()
        reopened = app.screen
        assert isinstance(reopened, ComposeScreen)
        async with asyncio.timeout(15):
            while reopened._attempt_pending:
                await asyncio.sleep(0.005)
        assert not reopened._has_unsaved_changes()
        assert reopened.query_one("#compose-attachment-list", Select).value == str(attachment)
        reopened.action_remove_attachment()
        await pilot.pause()
        assert app._draft_from_payload(reopened._payload(action="save")).attachment_paths == []


@pytest.mark.asyncio
async def test_confirmed_send_replaces_previous_status_before_worker(
    tmp_path: Path, monkeypatch
) -> None:
    app = GUCmailApp(_context(tmp_path), offline=True)
    pending = []
    monkeypatch.setattr(app, "submit_composer", lambda payload, **_kwargs: pending.append(payload))
    async with app.run_test(size=(140, 45)) as pilot:
        app._set_status("Previous unrelated error")
        app._open_compose(
            DraftData(
                reference="synthetic-send",
                to=[Address("synthetic@example.test")],
                subject="Synthetic pending-send check",
                body_text="Synthetic message",
            )
        )
        await pilot.pause()
        async with asyncio.timeout(15):
            while app.screen._attempt_pending:
                await asyncio.sleep(0.005)
        app.screen.action_review_send()
        async with asyncio.timeout(15):
            while not isinstance(app.screen, ConfirmationScreen):
                await asyncio.sleep(0.005)
        await pilot.pause()
        assert isinstance(app.screen, ConfirmationScreen)
        await pilot.press("y")
        await pilot.pause()
        assert len(pending) == 1 and pending[0]["action"] == "send"
        assert isinstance(app.screen, ComposeScreen) and app.screen._submission_pending
        status = str(app.screen_stack[0].query_one("#status-bar", Static).render())
        assert "waiting" in status.casefold()
        assert "Previous unrelated error" not in status


@pytest.mark.asyncio
async def test_attachment_workspace_opens_requested_tab_and_fits_toolbar(tmp_path: Path) -> None:
    from textual.widgets import TabbedContent

    from gucmail.services.drafts import DraftService
    from gucmail.tui.drafts_attachments_screen import DraftAttachmentScreen

    context = _context(tmp_path)
    path = tmp_path / "synthetic.txt"
    path.write_text("Synthetic local attachment", encoding="utf-8")
    DraftService(context.state, context.paths.recovery_dir).create(
        DraftData(reference="synthetic-local-draft", attachment_paths=[path])
    )
    app = GUCmailApp(context, offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._open_draft_attachment_workspace("synthetic-local-draft")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DraftAttachmentScreen)
        assert screen.query_one(TabbedContent).active == "draft-workspace-attachments-tab"
        await wait_until(
            pilot,
            lambda: screen.query_one("#attachment-table", DataTable).row_count,
            interval=0.03,
        )
        assert screen.query_one("#attachment-table", DataTable).row_count == 1
        for width, height in ((140, 45), (80, 24)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            toolbar = screen.query_one("#attachment-toolbar")
            for selector in ("#attachment-owner", "#attachment-filter", "#attachment-load"):
                widget = screen.query_one(selector)
                assert widget.region.width >= 16
                assert widget.region.right <= toolbar.region.right
            collision = screen.query_one("#attachment-collision")
            hash_row = screen.query_one("#attachment-hash-row")
            assert collision.region.bottom <= hash_row.region.y
            checkbox = screen.query_one("#attachment-calculate-hash", Checkbox)
            assert checkbox.content_size.width >= cell_len(str(checkbox.label)) + 4


def test_normal_watcher_shutdown_is_informational(tmp_path: Path) -> None:
    from threading import Event

    from gucmail.tui.area_backends import ActivityAreaBackend

    context = _context(tmp_path)
    assert context.state.claim_watcher_lease(
        scope="Inbox",
        lease_id="synthetic",
        owner_id="synthetic",
        process_id=123,
        profile_id="default",
        ttl_seconds=120,
        strategy="poll",
    )
    assert context.state.release_watcher_lease("Inbox", "synthetic", state="stopped")
    snapshot = ActivityAreaBackend.from_context(context, offline=True).load_activity(
        category="connection",
        text=None,
        limit=10,
        cancel=Event(),
        progress=lambda _: None,
    )
    event = next(item for item in snapshot.events if item.category == "connection")
    assert "disconnected" in event.summary.casefold()
    assert event.severity == "info"


@pytest.mark.asyncio
async def test_settings_show_catalog_names_but_keep_exact_choice_values(tmp_path: Path) -> None:
    from gucmail.tui.settings_screen import SettingsScreen

    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(140, 45)) as pilot:
        app._open_area("settings")
        await pilot.pause()
        await app.workers.wait_for_complete()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await wait_until(pilot, lambda: screen._catalog is not None, interval=0.05)
        assert screen._catalog is not None, str(screen.query_one("#area-state", Static).render())
        assert screen.action_select_setting("appearance.branding")
        await pilot.pause()
        detail = str(screen.query_one("#settings-detail", Static).render())
        assert "builtin.guc-mail-minimal" not in detail
        assert app.branding_browser_backend.catalog.get("builtin.guc-mail-minimal").name in detail
        assert screen.query_one("#settings-choice", Select).value == "builtin.guc-mail-minimal"
        screen.query_one("#settings-level", Select).value = "advanced"
        await pilot.pause()
        assert screen.action_select_setting("appearance.branding")
        await pilot.pause()
        detail = str(screen.query_one("#settings-detail", Static).render())
        assert "Raw value: builtin.guc-mail-minimal" in detail
        assert screen._staged == {}


@pytest.mark.asyncio
async def test_normalized_contact_labels_use_friendly_choices_without_losing_values(tmp_path):
    from gucmail.models import ContactInfo
    from gucmail.tui.contact_fields import ContactValueRow, ContactValuesEditor
    from gucmail.tui.pim_screens import ContactEditorScreen

    contact = ContactInfo(
        "synthetic",
        "Synthetic",
        phones={"businessphone": "00000"},
        addresses={"home": "Synthetic address"},
    )
    app = GUCmailApp(_context(tmp_path), offline=True)
    async with app.run_test(size=(110, 40)) as pilot:
        screen = ContactEditorScreen(contact)
        app.push_screen(screen)
        await pilot.pause()
        phones = screen.query_one("#contact-edit-phones", ContactValuesEditor)
        assert phones.choices.count(("Work", "BusinessPhone")) == 1
        assert ("businessphone", "businessphone") not in phones.choices
        assert phones.query_one(ContactValueRow).query_one(Select).value == "BusinessPhone"
        assert screen._value().phones == {"BusinessPhone": "00000"}
        assert screen._value().addresses == {"Home": "Synthetic address"}
        await pilot.press("escape")
        assert app.screen is not screen


def _rendered_brand_contrast(svg: str) -> dict[str, float]:
    """Contrast of each rendered G/U/C cell against its own background cell."""

    import re

    from gucmail.tui.themes import contrast_ratio

    prefix = re.search(r"\.(terminal-[\w-]+)-r\d+ \{", svg)
    assert prefix is not None
    classes = dict(re.findall(r"\.(terminal-[\w-]+-r\d+) \{ ([^}]*)\}", svg))
    backgrounds = {
        x: fill
        for fill, x, y in re.findall(
            r'<rect fill="(#[0-9a-fA-F]{6})" x="([\d.]+)" y="([\d.]+)"', svg
        )
        if float(y) < 20
    }
    result: dict[str, float] = {}
    for css_class, x, letter in re.findall(
        r'<text class="([^"]+)" x="([\d.]+)" y="20"[^>]*>([GUC])</text>', svg
    ):
        if letter in result:
            continue
        fill = re.search(r"fill: (#[0-9a-fA-F]{6})", classes[css_class])
        assert fill is not None, css_class
        result[letter] = contrast_ratio(fill.group(1), backgrounds[x])
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["builtin.default-dark", "builtin.default-light"])
@pytest.mark.parametrize("no_color_env", [False, True])
async def test_every_brand_letter_is_legible_in_default_themes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    theme: str,
    no_color_env: bool,
) -> None:
    # Regression: with NO_COLOR, Textual greyscales every cell, which turned the
    # German-red "U" into a grey almost identical to the dark header.
    if no_color_env:
        monkeypatch.setenv("NO_COLOR", "1")
    app = GUCmailApp(_context(tmp_path, theme=theme), offline=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        contrast = _rendered_brand_contrast(app.export_screenshot())
        header = app.query_one("#compact-title").parent
        assert header is not None
        assert header.has_class("monochrome-brand") is no_color_env

    assert set(contrast) == {"G", "U", "C"}
    for letter, ratio in contrast.items():
        assert ratio >= 3.0, f"{theme} {letter} contrast {ratio:.2f}"


@pytest.mark.parametrize("theme", ["builtin.default-dark", "builtin.default-light"])
def test_slim_brand_letters_meet_contrast_for_default_themes(theme: str) -> None:
    from gucmail.tui.themes import ColorCapability, contrast_ratio

    definition = ThemeRegistry().get(theme)
    for capability in (ColorCapability.TRUECOLOR, ColorCapability.COLORS_256):
        spans = slim_brand(definition, capability=capability).lines[0].spans[:3]
        assert [span.text for span in spans] == ["G", "U", "C"]
        for span in spans:
            assert span.foreground is not None
            assert contrast_ratio(span.foreground, definition.palette.header) >= 3.0
