"""The review describes the actual local action and next confirmation step."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import Button, Input, ListView, Static

from gucmail.commands import CommandContext, CommandInvocation
from gucmail.tui.app import (
    GUCmailApp,
    PaletteParameterReviewScreen,
    PaletteScreen,
    PaletteSequenceItem,
    PaletteSequencePreview,
    PaletteSequencePreviewScreen,
)
from gucmail.tui.settings_screen import SettingsScreen
from test_tui import FakeGateway
from test_tui_visual_regressions import _context


def test_named_theme_interpretation_describes_its_local_effect() -> None:
    palette = PaletteScreen("Africa/Cairo", command_context=CommandContext(offline_mode=True))
    result = palette.engine.interpret("Apply Default Light theme")
    assert result.invocation is not None
    assert result.invocation.command_id == "appearance.apply_theme"
    detail = palette._interpretation_detail(result)
    assert "changes app data" in detail
    assert "local/server" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence, label", [(1.0, "Apply"), (0.5, "Review confirmation")])
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_local_theme_parameter_review_labels_the_actual_next_action(
    tmp_path: Path, confidence: float, label: str, size: tuple[int, int]
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    palette = PaletteScreen("Africa/Cairo", command_context=CommandContext(offline_mode=True))
    invocation = CommandInvocation.from_mapping(
        "appearance.apply_theme", {"theme": "Default Light"}, confidence=confidence
    )
    plan = palette.engine.plan(invocation, context=palette.runtime_context)
    assert plan.confirmation_required is (confidence < 0.82)
    review = PaletteParameterReviewScreen(
        plan.command,
        invocation,
        plan.preview,
        coercer=lambda values: invocation,
        confirmation_required=plan.confirmation_required,
    )
    try:
        async with app.run_test(size=size) as pilot:
            await app.push_screen(review)
            await pilot.pause()
            assert str(review.query_one("#parameter-review-continue", Button).label) == label
            dialog = review.query_one("#parameter-review-box")
            for widget_id in ["parameter-review-cancel", "parameter-review-continue"]:
                button = review.query_one("#" + widget_id, Button)
                assert dialog.content_region.contains_region(button.region)
            for _ in range(5):
                if app.focused is review.query_one("#parameter-review-continue"):
                    break
                await pilot.press("tab")
            assert app.focused is review.query_one("#parameter-review-continue")
            assert dialog.content_region.contains_region(app.focused.region)
            assert not app.theme_session.is_previewing
            assert context.config_store.load().theme == app.active_theme.id
    finally:
        context.close()


def test_real_cli_projection_metadata_survives_concise_palette_details() -> None:
    from gucmail.cli import app as cli_app
    from gucmail.commands import discover_typer_registry
    from gucmail.palette import PaletteSuggestion, SuggestionSource

    screen = PaletteScreen("Africa/Cairo", command_context=CommandContext(offline_mode=True))
    screen.engine.planner.cli_registry = discover_typer_registry(cli_app)
    for command_id, parameters, projected in (
        ("backup.verify", {"reference": "synthetic-archive"}, True),
        ("layout.preview_wider", {}, False),
    ):
        invocation = CommandInvocation.from_mapping(command_id, parameters)
        plan = screen.engine.plan(invocation, context=screen.runtime_context)
        assert plan.cli.available is projected
        suggestion = PaletteSuggestion(
            plan.command, 1.0, SuggestionSource.SUGGESTED,
            plan.availability, invocation=invocation,
        )
        detail = screen._suggestion_detail(suggestion)
        if projected:
            assert plan.cli.argv == ("backup", "verify", "synthetic-archive")
            assert "gucmail " + " ".join(plan.cli.argv) in detail
        else:
            assert plan.cli.argv is None and plan.cli.reason
            assert "gucmail " not in detail and "No exact CLI projection" not in detail
        assert screen.engine.plan(invocation, context=screen.runtime_context) == plan


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_attachment_sequence_review_keeps_actions_visible(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    context = _context(tmp_path)
    app = GUCmailApp(context, offline=True)
    palette = PaletteScreen("Africa/Cairo")
    sequence = palette.engine.interpret_sequence(
        "find all PDFs from Ahmed then save them to Downloads"
    )
    preview = PaletteSequencePreview(
        sequence,
        (PaletteSequenceItem("attachment", "a-synthetic", "m-synthetic", "report.pdf", 13),),
        "Exchange applies 2 criterion/criteria first. Attachment filenames require local "
        "metadata. At most 200 candidates are checked locally.",
    )
    assert preview.target_command_id == "attachments.download_all_attachments"
    screen = PaletteSequencePreviewScreen(preview, destination=tmp_path / "owned-downloads")
    try:
        async with app.run_test(size=size) as pilot:
            await app.push_screen(screen)
            await pilot.pause()
            for widget_id in ["sequence-cancel", "sequence-continue"]:
                button = screen.query_one("#" + widget_id, Button)
                assert screen.query_one("#sequence-box").content_region.contains_region(
                    button.region
                )
            assert not (tmp_path / "owned-downloads").exists()
    finally:
        context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("size,theme", [
    ((80, 24), "builtin.default-dark"),
    ((120, 36), "builtin.default-light"),
    ((70, 20), "builtin.high-contrast-dark"),
    ((160, 48), "builtin.monochrome-gray"),
    ((100, 30), "builtin.solarized-light"),
    ((200, 60), "builtin.rose-dark"),
])
async def test_actual_palette_keeps_distinct_details_close_hint_and_settings_handoff(
    tmp_path, monkeypatch, size, theme
):
    context = _context(tmp_path)
    context.config = replace(context.config, theme=theme)
    context.config_store.save(context.config)
    monkeypatch.setattr(context.credential_resolver, "resolve",
                        lambda *a, **kw: pytest.fail("Palette resolved credentials"))
    app = GUCmailApp(context, gateway=FakeGateway(), offline=True)
    try:
        async with app.run_test(size=size) as pilot:
            mail = app.screen
            await pilot.press("ctrl+k")
            await pilot.pause()
            assert isinstance(app.screen, PaletteScreen)
            screen = app.screen
            assert screen.query_one("#palette-suggestions", ListView).children
            await pilot.press(*"settings")
            await pilot.pause()
            assert app.focused is screen.query_one("#palette-input", Input)
            assert str(screen.query_one("#palette-explanation", Static).render()).casefold().count(
                "open settings"
            ) == 1
            labels = [str(label.render()) for label in screen.query(".palette-item Label")]
            assert labels and all(label.casefold().count("open settings") == 1 for label in labels)
            await pilot.press("down")
            await pilot.pause()
            assert app.focused is screen.query_one("#palette-suggestions", ListView)
            assert str(screen.query_one("#palette-explanation", Static).render()).casefold().count(
                "open settings"
            ) == 1
            hint = screen.query_one("#palette-hint", Static)
            assert "Esc Close" in str(hint.render())
            assert screen.query_one("#palette-box").content_region.contains_region(hint.region)
            await pilot.press("enter")
            async with asyncio.timeout(15):
                while (not isinstance(app.screen, SettingsScreen) or app.screen._operation_active
                       or app.screen._render_pending
                       or not app.screen.query("#settings-form SettingRow")):
                    await asyncio.sleep(0.005)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is mail
            assert context.config_store.load().theme == theme
            await pilot.press("ctrl+k")
            await pilot.press(*"reply")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PaletteScreen)
            unavailable = [s for s in screen._display_suggestions
                           if s and not s.availability.available]
            assert unavailable and any(s.availability.reasons for s in unavailable)
            await pilot.press("down")
            await pilot.pause()
            assert "connection" in str(screen.query_one("#palette-explanation", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is mail
    finally:
        context.close()
