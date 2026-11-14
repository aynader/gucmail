"""The offline Help screen."""

from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
)

from gucmail.errors import (
    ConfigurationError,
)
from gucmail.help import (
    HelpNavigator,
    HelpPage,
    HelpRepository,
    HelpRuntimeContext,
    HelpStateStore,
    HelpUserState,
    parse_help_link,
)
from gucmail.settings import SettingsService
from gucmail.tui.literal_select import LiteralSelect as Select
from gucmail.tui.shell_widgets import CompactHeader
from gucmail.tui.shortcuts_screen import (
    ShortcutsRequested,
)
from gucmail.tui.stable_footer import StableFooter as Footer


class SettingsHelpStateStore:
    """Mirror real Help state into editable Settings without creating two truths."""

    def __init__(self, state: HelpStateStore, settings: SettingsService) -> None:
        self.state = state
        self.settings = settings

    def load_help_state(self) -> HelpUserState:
        stored = self.state.load_help_state()
        try:
            favorites = self.settings.value("help.favorites")
            recent = self.settings.value("help.recent_pages")
        except ConfigurationError:
            return stored
        favorite_ids = (
            frozenset(str(value) for value in favorites.effective_value)
            if favorites.source.value != "default"
            and isinstance(favorites.effective_value, (list, tuple))
            else stored.favorite_page_ids
        )
        recent_ids = (
            tuple(str(value) for value in recent.effective_value)
            if recent.source.value != "default"
            and isinstance(recent.effective_value, (list, tuple))
            else stored.recent_page_ids
        )
        mirrored = HelpUserState(
            recent_page_ids=recent_ids,
            favorite_page_ids=favorite_ids,
            recent_queries=stored.recent_queries,
        )
        if mirrored != stored:
            self.state.save_help_state(mirrored)
        return mirrored

    def save_help_state(self, state: HelpUserState) -> None:
        self.state.save_help_state(state)
        try:
            session = self.settings.begin()
            session.set("help.favorites", sorted(state.favorite_page_ids))
            session.set("help.recent_pages", list(state.recent_page_ids))
            session.apply(reason="Help favorites and history updated")
        except (ConfigurationError, OSError, ValueError):
            return


class HelpCommandRequested(Message):
    """Request a safe palette preview for a command link from Help."""

    def __init__(self, command_id: str) -> None:
        super().__init__()
        self.command_id = command_id


class HelpSettingRequested(Message):
    """Request the exact Settings row linked from Help."""

    def __init__(self, setting_id: str) -> None:
        super().__init__()
        self.setting_id = setting_id


class HelpTutorialsRequested(Message):
    def __init__(self, page_id: str) -> None:
        super().__init__()
        self.page_id = page_id


class HelpScreen(Screen[None]):
    """Searchable, offline documentation center backed by packaged Help pages."""

    DEFAULT_CSS = """
    HelpScreen #help-toolbar {
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    HelpScreen #help-toolbar Button { min-width: 5; }
    HelpScreen #help-search { width: 1fr; }
    HelpScreen #help-category { width: 28; }
    HelpScreen #help-body { height: 1fr; }
    HelpScreen #help-index {
        width: 36%;
        min-width: 28;
        max-width: 54;
        border-right: solid $primary-background;
    }
    HelpScreen #help-results { height: 1fr; }
    HelpScreen .help-result { height: auto; min-height: 4; padding: 0 1; }
    HelpScreen .help-result Label { width: 1fr; height: auto; text-wrap: wrap; }
    HelpScreen #help-article-pane { width: 1fr; height: 1fr; }
    HelpScreen #help-breadcrumbs {
        height: 2;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
    }
    HelpScreen #help-article-scroll { height: 1fr; padding: 0 2; }
    HelpScreen #help-article { width: 100%; }
    HelpScreen #help-notice {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $panel;
    }
    HelpScreen.narrow-help #help-body { layout: vertical; }
    HelpScreen.narrow-help #help-index {
        width: 100%; min-width: 1; max-width: 100%; height: 36%; min-height: 6;
        border-right: none; border-bottom: solid $primary-background;
    }
    HelpScreen.narrow-help #help-article-pane { width: 100%; height: 1fr; }
    HelpScreen.narrow-help #help-category { display: none; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("?", "dismiss", "Close"),
        Binding("ctrl+f", "focus_search", "Search"),
        Binding("alt+left", "back", "Back", show=False),
        Binding("alt+right", "forward", "Forward", show=False),
        Binding("ctrl+home", "home", "Help home", show=False),
        Binding("ctrl+d", "favorite", "Favorite", show=False),
        Binding("alt+t", "tutorials", "Tutorials", show=False),
    ]

    def __init__(
        self,
        *,
        context: HelpRuntimeContext | None = None,
        initial_page_id: str | None = None,
        initial_query: str = "",
        repository: HelpRepository | None = None,
        store: HelpStateStore | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository or HelpRepository.load_default()
        self.navigator = HelpNavigator(self.repository, store=store)
        self.help_context = context or HelpRuntimeContext(active_view="home")
        self.initial_page_id = initial_page_id
        self.initial_query = " ".join(initial_query.split())
        self._result_page_ids: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield CompactHeader("Help Center · offline documentation")
        categories = (
            ("All topics", ""),
            *((category, category) for category in self.repository.categories),
        )
        with Horizontal(id="help-toolbar"):
            yield Button("←", id="help-back", tooltip="Back (Alt+Left)")
            yield Button("→", id="help-forward", tooltip="Forward (Alt+Right)")
            yield Button("Home", id="help-home")
            yield Button("Tutorials", id="help-tutorials", tooltip="Guided practice (Alt+T)")
            yield Button("Keys", id="help-shortcuts", tooltip="Browse current keyboard bindings")
            yield Input(
                value=self.initial_query,
                placeholder="Search Help, errors, commands, settings, or ask a question…",
                id="help-search",
            )
            yield Select(categories, value="", allow_blank=False, id="help-category")
            yield Button("☆", id="help-favorite", tooltip="Favorite this article (Ctrl+D)")
        with Horizontal(id="help-body"):
            with Vertical(id="help-index"):
                yield ListView(id="help-results")
            with Vertical(id="help-article-pane"):
                yield Static("Help", id="help-breadcrumbs")
                with VerticalScroll(id="help-article-scroll"):
                    yield Markdown("Loading Help…", id="help-article", open_links=False)
        yield Static(
            "Type to search · Enter opens the best result · internal links stay in Help",
            id="help-notice",
        )
        yield Footer()

    async def on_mount(self) -> None:
        page_id = self.initial_page_id or self.repository.home_page_id
        try:
            page = self.navigator.open(page_id)
        except KeyError:
            page = self.navigator.home()
        await self._render_page(page.page_id, add_to_history=False)
        await self._refresh_results(self.initial_query)
        self.query_one("#help-search", Input).focus()
        self.set_class(self.size.width < 90, "narrow-help")

    def on_resize(self, event: events.Resize) -> None:
        self.set_class(event.size.width < 90, "narrow-help")

    async def on_screen_resume(self) -> None:
        if not self.is_mounted or self.navigator.current is None:
            return
        refresh_context = getattr(self.app, "_help_runtime_context", None)
        if callable(refresh_context):
            self.help_context = refresh_context(self.help_context.active_view or "help")
            await self._render_page(self.navigator.current.page_id, add_to_history=False)

    def _default_pages(self) -> tuple[HelpPage, ...]:
        """Return deduplicated context, recent, favorite, and start-here pages."""

        ordered: list[HelpPage] = []
        active_view = self.help_context.active_view
        if active_view:
            ordered.extend(
                self.repository.pages_for_context(
                    active_view,
                    platform=self.help_context.platform,
                )
            )
        for page_id in self.navigator.state.recent_page_ids:
            try:
                ordered.append(self.repository.get(page_id))
            except KeyError:
                continue
        ordered.extend(self.navigator.favorite_pages())
        ordered.extend(self.repository.pages_in_category("Start Here"))
        ordered.append(self.repository.get(self.repository.home_page_id))
        seen: set[str] = set()
        result: list[HelpPage] = []
        for page in ordered:
            if page.page_id in seen:
                continue
            seen.add(page.page_id)
            result.append(page)
        return tuple(result[:20])

    async def _refresh_results(self, query: str, *, record_query: bool = False) -> None:
        cleaned = " ".join(query.split())
        if cleaned:
            matches = (
                self.navigator.search(cleaned, limit=30, context=self.help_context)
                if record_query
                else self.repository.search(cleaned, limit=30, context=self.help_context)
            )
            pages = tuple(match.page for match in matches)
        else:
            pages = self._default_pages()
        self._result_page_ids = tuple(page.page_id for page in pages)
        recent = set(self.navigator.state.recent_page_ids)
        favorites = self.navigator.state.favorite_page_ids
        items: list[ListItem] = []
        for page in pages:
            markers = ("★" if page.page_id in favorites else "") + (
                "↶" if page.page_id in recent else ""
            )
            title = f"{markers + ' ' if markers else ''}{page.category} · {page.title}"
            items.append(
                ListItem(
                    Label(f"{title}\n{page.summary}", markup=False),
                    classes="help-result",
                )
            )
        if not items:
            items.append(
                ListItem(
                    Label(
                        "No Help topic matched\n"
                        "Try fewer words, an error message, or a feature name.",
                        markup=False,
                    ),
                    classes="help-result",
                    disabled=True,
                )
            )
        results = self.query_one("#help-results", ListView)
        await results.clear()
        await results.extend(items)
        results.index = 0 if self._result_page_ids else None

    async def _render_page(self, page_id: str, *, add_to_history: bool = True) -> None:
        page = self.navigator.open(page_id) if add_to_history else self.repository.get(page_id)
        rendered = self.repository.render(page.page_id, context=self.help_context)
        breadcrumbs = "  >  ".join(self.navigator.breadcrumbs())
        self.query_one("#help-breadcrumbs", Static).update(breadcrumbs)
        await self.query_one("#help-article", Markdown).update(rendered.markdown)
        favorite = page.page_id in self.navigator.state.favorite_page_ids
        self.query_one("#help-favorite", Button).label = "★" if favorite else "☆"
        self.query_one("#help-back", Button).disabled = not self.navigator.can_go_back
        self.query_one("#help-forward", Button).disabled = not self.navigator.can_go_forward
        self.query_one("#help-notice", Static).update(
            f"{page.category} · {page.skill_level.value.title()} · {rendered.availability_detail}"
        )

    @on(Input.Changed, "#help-search")
    async def search_changed(self, event: Input.Changed) -> None:
        await self._refresh_results(event.value)

    @on(Input.Submitted, "#help-search")
    async def search_submitted(self, event: Input.Submitted) -> None:
        await self._refresh_results(event.value, record_query=True)
        if self._result_page_ids:
            await self._render_page(self._result_page_ids[0])

    @on(Select.Changed, "#help-category")
    async def category_changed(self, event: Select.Changed) -> None:
        category = event.value if isinstance(event.value, str) else ""
        if not category:
            await self._refresh_results(self.query_one("#help-search", Input).value)
            return
        pages = self.repository.pages_in_category(category)
        self._result_page_ids = tuple(page.page_id for page in pages)
        items = [
            ListItem(
                Label(f"{page.title}\n{page.summary}", markup=False),
                classes="help-result",
            )
            for page in pages
        ]
        results = self.query_one("#help-results", ListView)
        await results.clear()
        await results.extend(items)
        results.index = 0 if items else None

    @on(ListView.Selected, "#help-results")
    async def result_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._result_page_ids):
            await self._render_page(self._result_page_ids[index])

    @on(Markdown.LinkClicked, "#help-article")
    async def help_link_clicked(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        try:
            link = parse_help_link(event.href)
        except ValueError:
            self.query_one("#help-notice", Static).update(
                "That Help link is invalid and was blocked."
            )
            return
        if link.scheme == "help":
            await self._render_page(link.identifier)
        elif link.scheme == "anchor":
            self.query_one("#help-article", Markdown).goto_anchor(link.identifier)
        elif link.scheme == "https":
            self.query_one("#help-notice", Static).update(
                "External link not opened automatically. Copy it from the article if needed."
            )
        elif link.scheme == "command":
            self.post_message(HelpCommandRequested(link.identifier))
            self.query_one("#help-notice", Static).update(
                f"Opening a safe palette preview for command:{link.identifier}."
            )
        elif link.scheme == "setting":
            self.post_message(HelpSettingRequested(link.identifier))
            self.query_one("#help-notice", Static).update(f"Opening setting:{link.identifier}.")
        else:
            self.query_one("#help-notice", Static).update(
                f"Use the command palette for {link.scheme}:{link.identifier}."
            )

    @on(Button.Pressed, "#help-back")
    async def back_pressed(self) -> None:
        await self.action_back()

    @on(Button.Pressed, "#help-forward")
    async def forward_pressed(self) -> None:
        await self.action_forward()

    @on(Button.Pressed, "#help-home")
    async def home_pressed(self) -> None:
        await self.action_home()

    @on(Button.Pressed, "#help-tutorials")
    def action_tutorials(self) -> None:
        page = self.navigator.current
        self.post_message(HelpTutorialsRequested(page.page_id if page else "start.interface"))

    @on(Button.Pressed, "#help-shortcuts")
    def shortcuts_pressed(self) -> None:
        self.post_message(ShortcutsRequested())

    @on(Button.Pressed, "#help-favorite")
    async def favorite_pressed(self) -> None:
        await self.action_favorite()

    def action_focus_search(self) -> None:
        self.query_one("#help-search", Input).focus()

    async def action_back(self) -> None:
        page = self.navigator.back()
        if page is not None:
            await self._render_page(page.page_id, add_to_history=False)

    async def action_forward(self) -> None:
        page = self.navigator.forward()
        if page is not None:
            await self._render_page(page.page_id, add_to_history=False)

    async def action_home(self) -> None:
        page = self.navigator.home()
        await self._render_page(page.page_id, add_to_history=False)

    async def action_favorite(self) -> None:
        page = self.navigator.current
        if page is None:
            return
        enabled = self.navigator.toggle_favorite(page.page_id)
        self.query_one("#help-favorite", Button).label = "★" if enabled else "☆"
        await self._refresh_results(self.query_one("#help-search", Input).value)
