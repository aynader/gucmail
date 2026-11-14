from __future__ import annotations

import asyncio
from threading import Event
from time import perf_counter

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from gucmail.tui.app import GUCmailApp, PaletteScreen
from gucmail.tui.area_common import ConfirmationScreen, ViewState
from gucmail.tui.ocr_screen import OcrScreen
from test_local_ocr import setup_image
from test_tui import context_for


async def wait(pilot, predicate, *, timeout=5):
    deadline = perf_counter() + timeout
    while perf_counter() < deadline:
        if predicate():
            return
        await pilot.pause(0.025)
    assert predicate(), f"OCR state was not reached: {type(pilot.app.screen).__name__}"


async def focus(pilot, selector):
    widget = pilot.app.screen.query_one(selector)
    for _ in range(40):
        if widget.has_focus:
            break
        await pilot.press("tab")
    await wait(
        pilot, lambda: widget.has_focus and pilot.app.screen.region.contains_region(widget.region)
    )
    return widget


async def activate(pilot, selector):
    button = await focus(pilot, selector)
    assert isinstance(button, Button) and not button.disabled
    await wait(pilot, lambda: not button.has_class("-active"))
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_palette_preview_opt_in_decline_accept_result_and_changed_input(
    tmp_path, monkeypatch, size
):
    import gucmail.tui.app_areas as module

    source, backend, service = setup_image(tmp_path)
    monkeypatch.setattr(module, "OcrService", lambda: service)
    context = context_for(tmp_path / "app")
    app = GUCmailApp(context, offline=True)
    monkeypatch.setattr(
        app, "_ensure_gateway", lambda: pytest.fail("Local OCR UI resolved Exchange")
    )
    try:
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+k")
            await wait(pilot, lambda: isinstance(app.screen, PaletteScreen))
            app.screen.query_one("#palette-input", Input).value = "Open Local Image OCR"
            await pilot.pause()
            await pilot.press("enter")
            await wait(
                pilot,
                lambda: isinstance(app.screen, OcrScreen) and not app.screen._operation_active,
            )
            screen = app.screen
            assert not screen.query_one("#ocr-enabled", Checkbox).value
            assert screen.query_one("#ocr-recognize", Button).disabled
            assert "en-US" in str(screen.query_one("#ocr-status", Static).content)
            screen.query_one("#ocr-path", Input).value = str(source)
            await pilot.pause()
            await focus(pilot, "#ocr-language")
            await pilot.press("enter", "end", "enter")
            await wait(pilot, lambda: screen.query_one("#ocr-language", Select).value == "en-US")
            await activate(pilot, "#ocr-preview-button")
            await wait(pilot, lambda: not screen._operation_active and screen._preview is not None)
            assert screen._preview.can_recognize and screen._preview.language == "en-US"
            assert screen.query_one("#ocr-recognize", Button).disabled
            assert all(c["mode"] == "status" for c in backend.calls)
            await focus(pilot, "#ocr-enabled")
            await pilot.press("space")
            await pilot.pause()
            await activate(pilot, "#ocr-recognize")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            assert (
                "No mailbox fetch" in app.screen.detail_text and "en-US" in app.screen.detail_text
            )
            await pilot.press("n")
            await wait(pilot, lambda: app.screen is screen)
            assert all(c["mode"] == "status" for c in backend.calls)
            await activate(pilot, "#ocr-recognize")
            await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
            await pilot.press("y")
            await wait(pilot, lambda: app.screen is screen and not screen._operation_active)
            await focus(pilot, "#ocr-result")
            assert screen.query_one("#ocr-result", TextArea).text == "Recognized synthetic image"
            assert "ocr_attachment_text" in str(screen.query_one("#ocr-preview", Static).content)
            assert backend.path is not None and not backend.path.exists()
            screen.query_one("#ocr-path", Input).value = "different image.png"
            await pilot.pause()
            assert screen._preview is None and not screen.query_one("#ocr-enabled", Checkbox).value
            assert screen.query_one("#ocr-result", TextArea).text == ""
            assert screen.query_one("#ocr-recognize", Button).disabled
            await activate(pilot, ".area-close")
            await wait(pilot, lambda: app.screen is app._screen_stack[0])
    finally:
        context.close()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unavailable_ocr_keeps_the_form_disabled_and_shows_installed_language_boundary(
    tmp_path,
):
    _, backend, service = setup_image(tmp_path)
    backend.languages = []
    app = App()
    screen = OcrScreen(service)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await wait(pilot, lambda: not screen._operation_active)
        assert "unavailable" in str(screen.query_one("#ocr-status", Static).content)
        assert screen.query_one("#ocr-recognize", Button).disabled
        assert not screen.query_one("#ocr-enabled", Checkbox).value
        await activate(pilot, "#ocr-preview-button")
        assert screen.view_state is ViewState.ERROR
        assert all(c["mode"] == "status" for c in backend.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["ctrl+c", "escape"])
async def test_cancel_or_close_signals_held_recognizer_and_removes_owned_image(tmp_path, cancel):
    source, backend, service = setup_image(tmp_path)
    started, finished = Event(), Event()
    original = backend.invoke
    observed = []

    def held(options, *, timeout, cancel_event):
        value = original(options, timeout=timeout, cancel_event=cancel_event)
        if options["mode"] == "extract":
            started.set()
            observed.append(cancel_event.wait(5))
            finished.set()
            return {"status": "cancelled"}
        return value

    backend.invoke = held
    app = App()
    screen = OcrScreen(service)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await wait(pilot, lambda: not screen._operation_active)
        screen.query_one("#ocr-path", Input).value = str(source)
        screen.query_one("#ocr-language", Select).value = "en-US"
        await pilot.pause()
        await activate(pilot, "#ocr-preview-button")
        await wait(pilot, lambda: screen._preview is not None and not screen._operation_active)
        await focus(pilot, "#ocr-enabled")
        await pilot.press("space")
        await activate(pilot, "#ocr-recognize")
        await wait(pilot, lambda: isinstance(app.screen, ConfirmationScreen))
        await pilot.press("y")
        await wait(pilot, started.is_set)
        assert backend.path is not None and backend.path.exists()
        await pilot.press(cancel)
        await wait(pilot, finished.is_set)
        await wait(pilot, lambda: not backend.path.exists())
        assert observed == [True] and source.exists()
        if cancel == "escape":
            assert app.screen is not screen
        else:
            assert app.screen is screen and screen.query_one("#ocr-result", TextArea).text == ""
