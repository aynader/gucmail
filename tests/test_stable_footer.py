"""Footer reuse preserves framework rendering, contextual dispatch and remapping."""

import asyncio
import gc
import weakref
from typing import ClassVar

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Input
from textual.widgets._footer import FooterKey

from gucmail.tui.stable_footer import StableFooter


async def settled(pilot, predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.005)
    await pilot.pause()


class ContextButton(Button):
    BINDINGS: ClassVar = [Binding("e", "local", "Local action", tooltip="Context-specific action")]

    def action_local(self):
        self.app.actions.append("local")


class FooterHost(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar = [Binding("g", "go", "Go", tooltip="Primary synthetic action"),
                Binding("x", "optional", "Optional")]

    def __init__(self, footer_type, *, contextual=False):
        super().__init__()
        self.footer_type = footer_type
        self.contextual = contextual
        self.available = True
        self.actions = []

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield (ContextButton if self.contextual else Button)("First", id="first")
            yield Button("Second", id="second")
            yield Input(id="editor")
        yield self.footer_type()

    def action_go(self):
        self.actions.append("go")

    def action_optional(self):
        self.actions.append("optional")

    def check_action(self, action, parameters):
        if action == "optional":
            return True if self.available else None
        return super().check_action(action, parameters)


def rendered(app):
    return tuple((key.key, key.key_display, key.description, key.action,
                  key._disabled, key.tooltip) for key in app.query(FooterKey))


async def ready(app, pilot):
    await settled(pilot, lambda: bool(app.query(FooterKey)))
    footer = app.query_one(Footer)
    if isinstance(footer, StableFooter):
        await settled(pilot, lambda: footer._rendered_bindings is not None
                      and not footer._recompose_lock.locked())
    await pilot.pause()


@pytest.mark.asyncio
async def test_identical_focus_hints_retain_existing_key_widgets():
    app = FooterHost(StableFooter)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        app.query_one("#first").focus()
        await pilot.pause()
        keys = tuple(app.query(FooterKey))
        initial = rendered(app)
        for key in ("tab", "shift+tab") * 5:
            await pilot.press(key)
            assert tuple(app.query(FooterKey)) == keys
            assert rendered(app) == initial
        assert app.actions == []


@pytest.mark.asyncio
async def test_contextual_footer_and_disabled_updates_match_framework():
    observations = []
    for footer_type in (Footer, StableFooter):
        app = FooterHost(footer_type, contextual=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await ready(app, pilot)
            current = []
            for identifier, available in (("first", True), ("second", True),
                                          ("second", False), ("first", False)):
                app.query_one("#" + identifier).focus()
                app.available = available
                app.refresh_bindings()
                await pilot.pause()
                await settled(
                    pilot, lambda app=app, available=available, identifier=identifier: any(
                        key.action == "optional" and key._disabled is (not available)
                        for key in app.query(FooterKey)
                    ) and any(key.action == "local" for key in app.query(FooterKey))
                        is (identifier == "first"),
                )
                current.append(rendered(app))
            app.query_one("#editor").focus()
            await settled(pilot, lambda app=app: not any(
                key.action == "optional" for key in app.query(FooterKey)
            ))
            current.append(rendered(app))
            observations.append(current)
    assert observations[0] == observations[1]


@pytest.mark.asyncio
async def test_footer_mouse_uses_current_namespace_and_disabled_action_does_not_run():
    app = FooterHost(StableFooter, contextual=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        app.query_one("#first").focus()
        await pilot.pause()
        local = next(key for key in app.query(FooterKey) if key.action == "local")
        assert await pilot.click(local)
        await settled(pilot, lambda: app.actions == ["local"])
        app.available = False
        app.refresh_bindings()
        await settled(pilot, lambda: any(
            key.action == "optional" and key._disabled for key in app.query(FooterKey)
        ))
        disabled = next(key for key in app.query(FooterKey) if key.action == "optional")
        assert await pilot.click(disabled)
        await pilot.pause()
        assert app.actions == ["local"]


@pytest.mark.asyncio
async def test_remap_replaces_key_and_click_dispatches_updated_binding():
    app = FooterHost(StableFooter)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        old_keys = tuple(app.query(FooterKey))
        app._bindings.key_to_bindings.pop("g")
        app._bindings.bind("b", "go", "Changed Go")
        app.refresh_bindings()
        await settled(pilot, lambda: any(key.key == "b" for key in app.query(FooterKey)))
        assert tuple(app.query(FooterKey)) != old_keys
        assert not any(key.key == "g" for key in app.query(FooterKey))
        current = next(key for key in app.query(FooterKey) if key.key == "b")
        assert current.description == "Changed Go"
        assert await pilot.click(current)
        await settled(pilot, lambda: app.actions == ["go"])


@pytest.mark.asyncio
async def test_interrupted_recompose_cannot_cache_a_partial_footer(monkeypatch):
    app = FooterHost(StableFooter)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        footer = app.query_one(StableFooter)
        expected = rendered(app)

        async def interrupted(widget):
            await widget.query_children().remove()
            await widget.mount(FooterKey("bad", "bad", "Partial", "bad"))
            raise asyncio.CancelledError

        app.available = False
        with monkeypatch.context() as patch:
            patch.setattr(Footer, "recompose", interrupted)
            with footer._context(), pytest.raises(asyncio.CancelledError):
                await footer.recompose()
        assert any(key.action == "bad" for key in app.query(FooterKey))
        app.available = True
        with footer._context():
            await footer.recompose()
        await pilot.pause()
        assert rendered(app) == expected


@pytest.mark.asyncio
async def test_initial_unready_render_is_rebuilt_when_bindings_become_ready():
    app = FooterHost(StableFooter)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        footer = app.query_one(StableFooter)
        expected = rendered(app)
        footer._bindings_ready = False
        footer._rendered_bindings = None
        with footer._context():
            await footer.recompose()
        assert not app.query(FooterKey)
        with footer._context():
            footer.bindings_changed(app.screen)
        await pilot.pause()
        assert rendered(app) == expected


@pytest.mark.asyncio
async def test_context_changes_release_old_keys_and_preserve_live_compact_watchers():
    app = FooterHost(StableFooter, contextual=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(app, pilot)
        previous = [weakref.ref(key) for key in app.query(FooterKey)]
        footer = app.query_one(StableFooter)
        changes = []

        def record_compact(value):
            changes.append(value)

        app.watch(footer, "compact", record_compact, init=False)
        for _ in range(20):
            for identifier in ("editor", "first"):
                app.query_one("#" + identifier).focus()
                await pilot.pause()
                await settled(pilot, lambda: footer._rendered_bindings
                              == footer._binding_signature()
                              and not footer._recompose_lock.locked())
        await pilot.pause()
        gc.collect()
        assert not any(reference() is not None for reference in previous)
        footer.compact = True
        await pilot.pause()
        assert changes == [True]
        assert all(key.compact for key in app.query(FooterKey))
        footer.compact = False
        await pilot.pause()
        assert changes == [True, False]
        assert all(not key.compact for key in app.query(FooterKey))
