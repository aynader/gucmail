"""The command palette (Ctrl+K) and its review screens."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from gucmail.commands import (
    AvailabilityState,
    CommandAvailability,
    CommandContext,
    CommandInvocation,
    CommandPlan,
    CommandSpec,
    ParameterSpec,
    ParameterType,
    ThreadResponseState,
)
from gucmail.dates import parse_datetime_expression
from gucmail.errors import (
    ConfigurationError,
)
from gucmail.formatting import human_bytes
from gucmail.models import (
    AttachmentInfo,
)
from gucmail.nlp import (
    DateRange,
    FolderEntity,
    InterpretationKind,
    NaturalLanguageResult,
    NaturalLanguageSequence,
    PersonEntity,
    SizeConstraint,
    extract_date_range,
    extract_exclusions,
    extract_ranking,
    extract_size_constraint,
    extract_thread_response_state,
)
from gucmail.palette import (
    PaletteEngine,
    PaletteSuggestion,
    SuggestionSource,
    universal_palette_registry,
)
from gucmail.security import sanitize_terminal
from gucmail.tui.dialogs import ConfirmationScreen
from gucmail.tui.literal_table import LiteralDataTable as DataTable
from gucmail.tui.palette_routes import (
    catalog_route,
)


@dataclass(frozen=True, slots=True)
class PaletteSubmission:
    """Typed palette result returned without converting it to CLI arguments."""

    invocation: CommandInvocation
    interpretation: NaturalLanguageResult
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PaletteSequenceSubmission:
    """A parser-allowlisted sequence that still requires a result preview."""

    sequence: NaturalLanguageSequence


@dataclass(frozen=True, slots=True)
class PaletteSequenceItem:
    """One bounded message or attachment retained for an exact result handoff."""

    kind: str
    reference: str
    owner_reference: str
    title: str
    size: int | None
    attachment: AttachmentInfo | None = None


@dataclass(frozen=True, slots=True)
class PaletteSequencePreview:
    """Read-only evidence collected before a chained follow-up can be confirmed."""

    sequence: NaturalLanguageSequence
    items: tuple[PaletteSequenceItem, ...]
    search_summary: str
    truncated: bool = False
    blocked_reason: str | None = None

    @property
    def target_command_id(self) -> str:
        invocation = self.sequence.steps[-1].invocation
        return invocation.command_id if invocation is not None else ""

    @property
    def known_bytes(self) -> int:
        return sum(item.size or 0 for item in self.items)

    @property
    def unknown_size_count(self) -> int:
        return sum(item.size is None for item in self.items)


@dataclass(frozen=True, slots=True)
class PaletteSequenceDecision:
    """User-reviewed continuation of a bounded sequence preview."""

    preview: PaletteSequencePreview
    destination: Path | None = None


class PaletteSequencePreviewScreen(ModalScreen[PaletteSequenceDecision | None]):
    """Show real result counts and editable destinations before any follow-up."""

    MAX_BULK_ATTACHMENT_BYTES = 500 * 1024 * 1024
    DEFAULT_CSS = """
    PaletteSequencePreviewScreen { align: center middle; }
    #sequence-box {
        width: 92%; max-width: 108; height: 86%; min-height: 16;
        border: thick $accent; background: $gucmail-modal-background; padding: 1 2;
    }
    #sequence-summary, #sequence-safety { height: auto; margin-bottom: 1; }
    #sequence-content { height: 1fr; }
    #sequence-results { height: 8; min-height: 5; }
    #sequence-destination { height: 3; margin-top: 1; }
    #sequence-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #sequence-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, preview: PaletteSequencePreview, *, destination: Path | None) -> None:
        super().__init__()
        self.preview = preview
        self.destination = destination

    def compose(self) -> ComposeResult:
        target = self.preview.target_command_id
        known = human_bytes(self.preview.known_bytes)
        unknown = self.preview.unknown_size_count
        qualifier = "at least " if self.preview.truncated else ""
        size_text = f"{known} known" + (f" + {unknown} unknown-size" if unknown else "")
        with Vertical(id="sequence-box"):
            yield Label("Review command sequence", classes="dialog-title")
            with VerticalScroll(id="sequence-content"):
                yield Static(
                    f"1. Search (read-only)\n2. {self._target_label(target)}\n\n"
                    f"Matched: {qualifier}{len(self.preview.items)} item(s)\n"
                    f"Estimated size: {size_text}\n{self.preview.search_summary}",
                    id="sequence-summary",
                    markup=False,
                )
                yield DataTable(id="sequence-results", cursor_type="row", zebra_stripes=True)
                if self._needs_destination(target):
                    yield Input(
                        value=str(self.destination or ""),
                        placeholder="Review destination directory",
                        id="sequence-destination",
                    )
                yield Static(self._safety_text(), id="sequence-safety", markup=False)
            with Horizontal(id="sequence-actions"):
                yield Button("Cancel", id="sequence-cancel")
                yield Button(
                    self._continue_label(target), id="sequence-continue", variant="primary"
                )

    def on_mount(self) -> None:
        table = self.query_one("#sequence-results", DataTable)
        table.add_columns("Type", "Item", "Size")
        for index, item in enumerate(self.preview.items[:100]):
            table.add_row(
                item.kind,
                sanitize_terminal(item.title, max_length=180),
                human_bytes(item.size),
                key=f"{index}:{item.owner_reference}:{item.reference}",
            )
        blocked = (
            bool(self.preview.blocked_reason)
            or not self.preview.items
            or (
                self.preview.target_command_id == "attachments.download_all_attachments"
                and (
                    self.preview.unknown_size_count > 0
                    or self.preview.known_bytes > self.MAX_BULK_ATTACHMENT_BYTES
                )
            )
        )
        self.query_one("#sequence-continue", Button).disabled = blocked

    @staticmethod
    def _needs_destination(command_id: str) -> bool:
        return command_id in {
            "backup.back_up_search_results",
            "attachments.download_all_attachments",
        }

    @staticmethod
    def _target_label(command_id: str) -> str:
        return {
            "mail.mark_read": "Mark the exact result set read",
            "mail.mark_unread": "Mark the exact result set unread",
            "backup.back_up_search_results": "Configure a selected-message backup",
            "attachments.download_all_attachments": "Download the exact attachment set",
        }.get(command_id, "Review follow-up")

    @staticmethod
    def _continue_label(command_id: str) -> str:
        return "Configure backup" if command_id == "backup.back_up_search_results" else "Continue"

    def _safety_text(self) -> str:
        if self.preview.blocked_reason:
            return self.preview.blocked_reason
        if not self.preview.items:
            return "No matching items; nothing can run."
        if self.preview.target_command_id == "attachments.download_all_attachments":
            if self.preview.unknown_size_count:
                return (
                    "Bulk download is blocked because every attachment needs a known size. "
                    "Use the attachment workspace to review unknown-size files individually."
                )
            if self.preview.known_bytes > self.MAX_BULK_ATTACHMENT_BYTES:
                return "Bulk download is blocked above the 500 MiB sequence limit."
            return (
                "No payload has been downloaded. Review the destination; a second exact "
                "confirmation is required and files are never opened automatically."
            )
        if self.preview.target_command_id == "backup.back_up_search_results":
            return (
                "Review the destination here, then review and confirm the immutable backup in "
                "Backup Center."
            )
        return "No message has changed. Continue to a separate exact-target confirmation."

    @on(Button.Pressed, "#sequence-actions Button")
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sequence-cancel":
            self.dismiss(None)
            return
        destination: Path | None = None
        if self._needs_destination(self.preview.target_command_id):
            raw = self.query_one("#sequence-destination", Input).value.strip()
            if not raw or any(ord(character) < 32 for character in raw):
                self.query_one("#sequence-safety", Static).update(
                    "Enter a valid destination directory before continuing."
                )
                return
            destination = Path(raw).expanduser().resolve(strict=False)
        self.dismiss(PaletteSequenceDecision(self.preview, destination))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PaletteParameterReviewScreen(ModalScreen[CommandInvocation | None]):
    """Editable, typed review of parameters interpreted for a mutating command."""

    DEFAULT_CSS = """
    PaletteParameterReviewScreen { align: center middle; }
    #parameter-review-box {
        width: 86%; max-width: 88; height: 88%; max-height: 28; min-height: 12;
        border: thick $warning; background: $gucmail-modal-background; padding: 1 2;
    }
    #parameter-review-summary, #parameter-review-error { height: auto; margin-bottom: 1; }
    #parameter-review-content { height: 1fr; }
    #parameter-review-fields { height: auto; }
    .parameter-review-label { height: 1; color: $text-muted; }
    .parameter-review-input { height: 3; margin-bottom: 1; }
    #parameter-review-actions { height: 3; align-horizontal: right; }
    #parameter-review-actions Button { margin-left: 1; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        command: CommandSpec,
        invocation: CommandInvocation,
        preview: Sequence[str],
        *,
        coercer: Callable[[Mapping[str, str]], CommandInvocation],
        confirmation_required: bool = True,
    ) -> None:
        super().__init__()
        self.command = command
        self.invocation = invocation
        self.preview = tuple(preview)
        self.coercer = coercer
        self.confirmation_required = confirmation_required
        self._parameters = invocation.parameters

    def compose(self) -> ComposeResult:
        with Vertical(id="parameter-review-box"):
            yield Label(f"Review · {self.command.name}", classes="dialog-title", markup=False)
            with VerticalScroll(id="parameter-review-content"):
                yield Static(
                    "Inspect or edit the interpreted values. Nothing has executed.\n"
                    + " · ".join(self.preview[:4]),
                    id="parameter-review-summary",
                    markup=False,
                )
                with Vertical(id="parameter-review-fields"):
                    for index, (name, value) in enumerate(self._parameters):
                        spec = next(
                            (
                                parameter
                                for parameter in self.command.parameters
                                if parameter.name == name
                            ),
                            None,
                        )
                        choices = (
                            f" · choices: {', '.join(spec.choices)}"
                            if spec and spec.choices
                            else ""
                        )
                        yield Label(
                            f"{name.replace('_', ' ').title()}{choices}",
                            classes="parameter-review-label",
                            markup=False,
                        )
                        yield Input(
                            value=self._editable_value(value),
                            id=f"parameter-review-{index}",
                            classes="parameter-review-input",
                        )
                yield Static("", id="parameter-review-error", markup=False)
            with Horizontal(id="parameter-review-actions"):
                yield Button("Cancel", id="parameter-review-cancel")
                yield Button(
                    "Review confirmation"
                    if self.confirmation_required
                    else "Apply"
                    if self.command.changes_local and not self.command.changes_server
                    else "Continue",
                    id="parameter-review-continue",
                    variant="primary",
                )

    @staticmethod
    def _editable_value(value: object) -> str:
        if isinstance(value, DateRange):
            start = value.start.isoformat() if value.start is not None else ""
            end = value.end.isoformat() if value.end is not None else ""
            return f"{start} .. {end}"
        if isinstance(value, SizeConstraint):
            if value.minimum_bytes is not None and value.maximum_bytes is not None:
                return f"between {value.minimum_bytes} bytes and {value.maximum_bytes} bytes"
            if value.minimum_bytes is not None:
                return f"larger than {value.minimum_bytes} bytes"
            return f"under {value.maximum_bytes or 0} bytes"
        if isinstance(value, (tuple, list)):
            return ", ".join(PaletteParameterReviewScreen._editable_value(item) for item in value)
        for attribute in ("local", "expression", "email", "canonical", "display"):
            candidate = getattr(value, attribute, None)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()
        if isinstance(value, bool):
            return str(value).lower()
        return str(value)

    @on(Button.Pressed, "#parameter-review-actions Button")
    def _button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "parameter-review-cancel":
            self.dismiss(None)
            return
        raw = {
            name: self.query_one(f"#parameter-review-{index}", Input).value.strip()
            for index, (name, _value) in enumerate(self._parameters)
        }
        try:
            invocation = self.coercer(raw)
        except (ConfigurationError, ValueError) as exc:
            self.query_one("#parameter-review-error", Static).update(
                sanitize_terminal(str(exc), max_length=500)
            )
            return
        self.dismiss(invocation)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PaletteScreen(ModalScreen[PaletteSubmission | PaletteSequenceSubmission | None]):
    """One Spotlight-style surface for commands, natural language, and mail search."""

    DEFAULT_CSS = """
    PaletteScreen { align: center top; }
    #palette-box {
        width: 92%; max-width: 100; height: 1fr; max-height: 21; min-height: 10;
        margin-top: 2;
        border: tall $accent; background: $gucmail-command-palette-background;
    }
    #palette-input { margin: 1 1 0 1; }
    #palette-explanation {
        height: auto; min-height: 2; max-height: 6; margin: 0 1; color: $text-muted;
        text-wrap: wrap;
    }
    #palette-suggestions { height: 1fr; min-height: 3; }
    .palette-category { height: 1; padding: 0 1; color: $accent; background: $boost; }
    .palette-item { height: auto; min-height: 2; padding: 0 1; }
    .palette-item Label { width: 1fr; height: auto; text-wrap: wrap; }
    #palette-hint {
        height: auto; min-height: 1; max-height: 2; padding: 0 1;
        color: $text-muted; background: $boost; text-wrap: wrap;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    CONTEXT_BINDINGS: ClassVar = (
        Binding("down", "focus_suggestions", "From query to matching suggestions"),
        Binding("up", "focus_query", "From first suggestion to query"),
    )

    def __init__(
        self,
        timezone: str,
        *,
        command_context: CommandContext | Mapping[str, str] | None = None,
        context_supplier: Callable[[], CommandContext] | None = None,
        folder_candidates: Sequence[str] = (),
        person_candidates: Sequence[str] = (),
        recent_command_ids: Sequence[str] = (),
        supplemental_suggestions: Sequence[PaletteSuggestion] = (),
        usage_counts: Mapping[str, int] | None = None,
        handler_ids: Sequence[str] | None = None,
        fuzzy_threshold: float = 0.68,
        natural_language: bool = True,
        suggestion_count: int = 18,
        show_descriptions: bool = True,
    ) -> None:
        super().__init__()
        self.timezone = timezone
        if isinstance(command_context, CommandContext):
            runtime = command_context
        else:
            legacy = dict(command_context or {})
            runtime = CommandContext.from_mapping(
                legacy,
                online=legacy.get("online", "").casefold() == "true",
                offline_mode=legacy.get("offline", "").casefold() == "true",
            )
        if handler_ids is not None:
            runtime = replace(runtime, handler_ids=frozenset(handler_ids))
        self.runtime_context = runtime
        self._context_supplier = context_supplier
        self._context_timer: Timer | None = None
        self.command_context = dict(runtime.values)
        self.folder_candidates = tuple(folder_candidates)
        self.person_candidates = tuple(person_candidates)
        self.recent_command_ids = tuple(dict.fromkeys(recent_command_ids))[:20]
        self.supplemental_suggestions = tuple(supplemental_suggestions)[:12]
        self.usage_counts = {
            command_id: max(int(count), 0)
            for command_id, count in (usage_counts or {}).items()
            if command_id in {command.command_id for command in universal_palette_registry().all()}
        }
        self.natural_language = natural_language
        self.suggestion_count = min(max(suggestion_count, 3), 50)
        self.show_descriptions = show_descriptions
        self.registry = universal_palette_registry()
        threshold = fuzzy_threshold / 100.0 if fuzzy_threshold > 1.0 else fuzzy_threshold
        self.engine = PaletteEngine(
            self.registry,
            timezone=self.timezone,
            fuzzy_threshold=min(max(threshold, 0.0), 1.0),
            usage_score=lambda command_id: min(
                self.usage_counts.get(command_id, 0) / 10.0,
                1.0,
            ),
        )
        self._interpretation = self.engine.interpret(
            "",
            context=self.runtime_context,
            folder_candidates=self.folder_candidates,
            person_candidates=self.person_candidates,
        )
        self._suggestions: tuple[PaletteSuggestion, ...] = ()
        self._display_suggestions: tuple[PaletteSuggestion | None, ...] = ()
        self._sequence: NaturalLanguageSequence | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Input(placeholder="Search or describe an action…", id="palette-input")
            yield Static(
                "Type a command, mail search, setting, or question. Interpretation stays local.",
                id="palette-explanation",
            )
            yield ListView(id="palette-suggestions")
            yield Static(
                "↑↓ Choose · Enter Run/open · Tab Focus · Esc Close",
                id="palette-hint",
            )

    async def on_mount(self) -> None:
        await self._update_interpretation("")
        self.query_one("#palette-input", Input).focus()
        if self._context_supplier is not None:
            self._context_timer = self.set_interval(0.25, self._refresh_availability)

    def on_unmount(self) -> None:
        if self._context_timer is not None:
            self._context_timer.stop()

    def _refresh_runtime_context(self) -> bool:
        if self._context_supplier is None:
            return False
        runtime = self._context_supplier()
        if runtime == self.runtime_context:
            return False
        self.runtime_context = runtime
        self.command_context = dict(runtime.values)
        return True

    async def _refresh_availability(self) -> None:
        if self.app.screen is self and self._refresh_runtime_context():
            await self._update_interpretation(
                self.query_one("#palette-input", Input).value, preserve_selection=True
            )

    @on(Input.Changed, "#palette-input")
    async def explain(self, event: Input.Changed) -> None:
        await self._update_interpretation(event.value)

    @on(Input.Submitted, "#palette-input")
    def submit(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        if self._sequence is not None and self._sequence.complete:
            self.dismiss(PaletteSequenceSubmission(self._sequence))
            return
        if self._interpretation.invocation is not None:
            self._submit_invocation(self._interpretation.invocation, self._interpretation)
            return
        suggestions = self.query_one("#palette-suggestions", ListView)
        index = suggestions.index or 0
        if 0 <= index < len(self._display_suggestions):
            suggestion = self._display_suggestions[index]
            if suggestion is not None:
                self._submit_suggestion(suggestion)

    @on(ListView.Selected, "#palette-suggestions")
    def suggestion_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._display_suggestions):
            suggestion = self._display_suggestions[index]
            if suggestion is not None:
                self._submit_suggestion(suggestion)

    @on(ListView.Highlighted, "#palette-suggestions")
    def suggestion_highlighted(self, event: ListView.Highlighted) -> None:
        if not event.list_view.has_focus:
            return
        index = event.list_view.index
        if index is None or not 0 <= index < len(self._display_suggestions):
            return
        suggestion = self._display_suggestions[index]
        if suggestion is not None:
            self.query_one("#palette-explanation", Static).update(
                self._suggestion_detail(suggestion)
            )

    def on_key(self, event: events.Key) -> None:
        suggestions = self.query_one("#palette-suggestions", ListView)
        if (
            event.key == self.CONTEXT_BINDINGS[0].key
            and isinstance(self.focused, Input)
            and self._suggestions
        ):
            suggestions.focus()
            index = suggestions.index
            if index is not None and 0 <= index < len(self._display_suggestions):
                suggestion = self._display_suggestions[index]
                if suggestion is not None:
                    self.query_one("#palette-explanation", Static).update(
                        self._suggestion_detail(suggestion)
                    )
            event.prevent_default()
            event.stop()
        elif (
            event.key == self.CONTEXT_BINDINGS[1].key
            and self.focused is suggestions
            and suggestions.index in {None, 0}
        ):
            self.query_one("#palette-input", Input).focus()
            event.prevent_default()
            event.stop()

    async def _update_interpretation(self, text: str, *, preserve_selection: bool = False) -> None:
        self._refresh_runtime_context()
        list_view = self.query_one("#palette-suggestions", ListView)
        index = list_view.index
        selected = (
            self._display_suggestions[index]
            if preserve_selection
            and index is not None
            and 0 <= index < len(self._display_suggestions)
            else None
        )
        self._sequence = None
        interpreted = self.engine.interpret(
            text,
            context=self.runtime_context,
            folder_candidates=self.folder_candidates,
            person_candidates=self.person_candidates,
        )
        if self.natural_language or not text.strip():
            self._interpretation = interpreted
            if text.strip():
                sequence = self.engine.interpret_sequence(
                    text,
                    context=self.runtime_context,
                    folder_candidates=self.folder_candidates,
                    person_candidates=self.person_candidates,
                )
                if sequence.complete and len(sequence.steps) > 1:
                    self._sequence = sequence
            rows = self.engine.suggest(
                text,
                context=self.runtime_context,
                folder_candidates=self.folder_candidates,
                person_candidates=self.person_candidates,
                recent_command_ids=self.recent_command_ids,
                limit=self.suggestion_count,
            )
        else:
            self._interpretation = NaturalLanguageResult(
                original=text,
                normalized=text.casefold(),
                kind=InterpretationKind.INCOMPLETE,
                invocation=None,
                candidates=(),
                entities=interpreted.entities,
                explanation="Natural-language interpretation is disabled; showing command matches.",
            )
            rows = tuple(
                PaletteSuggestion(
                    command=command,
                    score=1.0,
                    source=SuggestionSource.COMMAND,
                    availability=self.registry.availability(
                        command.command_id, self.runtime_context
                    ),
                    reason="Command metadata match",
                )
                for command in self.registry.search(text, limit=self.suggestion_count)
            )
        rows = tuple(self._apply_route_availability(row) for row in rows)
        if not text.strip() and self.supplemental_suggestions:
            suggested_rows = tuple(row for row in rows if row.group == "Suggested actions")
            ordinary_rows = tuple(row for row in rows if row.group != "Suggested actions")
            rows = (
                *suggested_rows,
                *tuple(
                    self._apply_route_availability(row) for row in self.supplemental_suggestions
                ),
                *ordinary_rows,
            )[: self.suggestion_count]
        categories: dict[str, list[PaletteSuggestion]] = {}
        for row in rows:
            count = self.usage_counts.get(row.command.command_id, 0)
            if row.group:
                category = row.group
            elif not text.strip() and count > 1:
                category = "Frequently used commands"
            elif not text.strip() and row.command.command_id in self.recent_command_ids:
                category = "Recently used commands"
            elif not text.strip() and row.command.category == self.command_context.get(
                "current_area"
            ):
                category = "Context-sensitive commands"
            else:
                category = row.command.category
            categories.setdefault(category, []).append(row)
        rows = tuple(row for group in categories.values() for row in group)
        explanation = (
            self._sequence_detail(self._sequence)
            if self._sequence is not None
            else self._interpretation_detail(self._interpretation)
        )
        self.query_one("#palette-explanation", Static).update(explanation)
        self._suggestions = rows
        recent = set(self.recent_command_ids)
        items: list[ListItem] = []
        display_rows: list[PaletteSuggestion | None] = []
        for category, suggestions_for_category in categories.items():
            items.append(
                ListItem(
                    Label(category.title(), markup=False),
                    classes="palette-category",
                    disabled=True,
                )
            )
            display_rows.append(None)
            for suggestion in suggestions_for_category:
                command = suggestion.command
                unavailable = (
                    "; ".join(suggestion.availability.reasons)
                    if not suggestion.availability.available
                    else ""
                )
                shortcut = f" · {command.shortcut}" if command.shortcut else ""
                count = self.usage_counts.get(command.command_id, 0)
                marker = (
                    f"Frequent x{count} · "
                    if count > 1
                    else "Recent · "
                    if command.command_id in recent
                    and suggestion.source
                    not in {
                        SuggestionSource.SUGGESTED,
                        SuggestionSource.RECENT_SEARCH,
                        SuggestionSource.RECENT_FOLDER,
                        SuggestionSource.RECENT_PERSON,
                    }
                    else ""
                )
                status = "Not available here" if unavailable else ""
                description = (
                    self._metadata_description(command.name, command.description)
                    if self.show_descriptions
                    else ""
                )
                detail = " · ".join(part for part in (description, status) if part)
                label = f"{marker}{suggestion.display_name or command.name}{shortcut}"
                if detail:
                    label += "\n" + detail
                items.append(
                    ListItem(
                        Label(label, markup=False),
                        classes="palette-item",
                    )
                )
                display_rows.append(suggestion)
        self._display_suggestions = tuple(display_rows)
        suggestions = self.query_one("#palette-suggestions", ListView)
        await suggestions.clear()
        await suggestions.extend(items)
        suggestions.index = next(
            (
                index
                for index, row in enumerate(display_rows)
                if selected is not None
                and row is not None
                and row.command.command_id == selected.command.command_id
                and row.display_name == selected.display_name
            ),
            1 if len(items) > 1 else None,
        )

    @staticmethod
    def _metadata_description(title: str, description: str) -> str:
        """Suppress a repeated trusted metadata title, preserving other detail."""

        def normalized(value: str) -> str:
            return " ".join(value.split()).rstrip(".").casefold()

        if normalized(title) == normalized(description):
            return ""
        prefix = title.strip().rstrip(".") + ". "
        if description.casefold().startswith(prefix.casefold()):
            return description[len(prefix) :].lstrip()
        return description

    def _interpretation_detail(self, result: NaturalLanguageResult) -> str:
        if result.invocation is None:
            return sanitize_terminal(result.explanation, max_length=500)
        plan = self.engine.plan(result.invocation, context=self.runtime_context)
        command = plan.command
        route_reason = self._route_gate_reason(command.command_id)
        if route_reason:
            return sanitize_terminal(
                f"→ {command.name} · not available here\n{route_reason}",
                max_length=1_000,
            )
        parameters = self._format_parameters(result.invocation)
        if command.command_id in {
            "search.search_current_folder",
            "search.search_all_mail",
            "search.mail",
        } and not result.invocation.get("query"):
            parameters = "Enter opens a prompt for message text."
        effect = (
            "changes your mailbox"
            if command.changes_server
            else "changes app data"
            if command.changes_local or command.mutates
            else ""
        )
        notes = [effect] if effect else []
        if plan.confirmation_required:
            notes.append("asks first")
        safety = (" · " + " · ".join(notes)) if notes else ""
        # The first two plan preview fields are this same name/description.
        # Keep the real plan intact for review; avoid repeating it in the heading.
        detail = parameters or self._metadata_description(command.name, command.description)
        heading = f"→ {command.name}{safety}"
        return sanitize_terminal(
            heading + ("\n" + detail if detail else ""),
            max_length=1_000,
        )

    def _sequence_detail(self, sequence: NaturalLanguageSequence) -> str:
        names = [
            self.registry.get(step.invocation.command_id).name
            for step in sequence.steps
            if step.invocation is not None
        ]
        first = sequence.steps[0]
        parameters = self._format_parameters(first.invocation) if first.invocation else ""
        return sanitize_terminal(
            "Sequence · "
            + " → ".join(names)
            + f"\n{parameters or 'No search parameters'}\n"
            + "Enter previews real matches; no follow-up runs before a second confirmation.",
            max_length=1_000,
        )

    def _suggestion_detail(self, suggestion: PaletteSuggestion) -> str:
        command = suggestion.command
        invocation = suggestion.invocation or CommandInvocation(command.command_id)
        plan = self.engine.plan(invocation, context=self.runtime_context)
        cli = "gucmail " + " ".join(plan.cli.argv) if plan.cli.available and plan.cli.argv else ""
        follow_ups = tuple(
            self.registry.get(command_id).name for command_id in command.follow_up_commands[:3]
        )
        next_actions = "Next: " + ", ".join(follow_ups) if follow_ups else ""
        availability = (
            "Available"
            if suggestion.availability.available
            else "; ".join(suggestion.availability.reasons)
        )
        detail = self._metadata_description(
            command.name, suggestion.reason or command.long_description
        )
        parts = (
            f"{suggestion.display_name or command.name} · {availability}",
            detail,
            cli,
            next_actions,
        )
        return sanitize_terminal("\n".join(part for part in parts if part), max_length=1_000)

    @staticmethod
    def _format_parameters(invocation: CommandInvocation) -> str:
        values = []
        parameters = tuple(invocation.parameters)
        for name, value in parameters:
            if name == "query" and len(parameters) > 1:
                # The other filters already say what the free text meant.
                continue
            display = (
                getattr(value, "display", None)
                or getattr(value, "expression", None)
                or getattr(value, "original", None)
                or getattr(value, "local", None)
                or value
            )
            if isinstance(display, bool):
                display = "Yes" if display else "No"
            values.append(f"{name.replace('_', ' ').capitalize()}: {display}")
        return " · ".join(values)

    def _coerce_reviewed_invocation(
        self,
        invocation: CommandInvocation,
        raw_parameters: Mapping[str, str],
    ) -> CommandInvocation:
        """Rebuild edited palette parameters using the command registry's declared types."""

        command = self.registry.get(invocation.command_id)
        specifications = {parameter.name: parameter for parameter in command.parameters}
        parameters: dict[str, object] = {}
        for name, raw_value in raw_parameters.items():
            specification = specifications.get(name)
            if specification is None:
                raise ConfigurationError(f"{name!r} is not a parameter of {command.name}.")
            if not raw_value.strip():
                if specification.required:
                    raise ConfigurationError(f"{name.replace('_', ' ').title()} cannot be empty.")
                continue
            parameters[name] = self._coerce_reviewed_parameter(specification, raw_value)
        revised = CommandInvocation.from_mapping(
            invocation.command_id,
            parameters,
            original_text=invocation.original_text,
            confidence=invocation.confidence,
            explanation=invocation.explanation,
        )
        errors = self.registry.validate_invocation(revised)
        if errors:
            raise ConfigurationError("; ".join(errors))
        return revised

    def _coerce_reviewed_parameter(self, specification: ParameterSpec, raw_value: str) -> object:
        if specification.multiple:
            parts = tuple(part.strip() for part in raw_value.split(",") if part.strip())
            if not parts:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} needs at least one value."
                )
            scalar = replace(specification, multiple=False)
            return tuple(self._coerce_reviewed_parameter(scalar, part) for part in parts)

        parameter_type = specification.type
        if parameter_type is ParameterType.INTEGER:
            try:
                return int(raw_value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} must be a whole number."
                ) from exc
        if parameter_type is ParameterType.NUMBER:
            try:
                return float(raw_value)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{specification.name.replace('_', ' ').title()} must be a number."
                ) from exc
        if parameter_type is ParameterType.BOOLEAN:
            normalized = raw_value.casefold()
            if normalized in {"true", "yes", "on", "1"}:
                return True
            if normalized in {"false", "no", "off", "0"}:
                return False
            raise ConfigurationError(
                f"{specification.name.replace('_', ' ').title()} must be true or false."
            )
        if parameter_type is ParameterType.PERSON:
            return PersonEntity(
                raw_value,
                raw_value if "@" in raw_value else None,
                source="palette_review",
            )
        if parameter_type is ParameterType.FOLDER:
            return FolderEntity(raw_value, raw_value, source="palette_review")
        if parameter_type is ParameterType.DATETIME:
            return parse_datetime_expression(
                raw_value,
                timezone=self.timezone,
                future_required=True,
            )
        if parameter_type is ParameterType.DATE_RANGE:
            if ".." not in raw_value:
                parsed_range = extract_date_range(raw_value, timezone=self.timezone)
                if parsed_range is None:
                    raise ConfigurationError(
                        "Date range must be a recognizable phrase or 'start .. end'."
                    )
                return parsed_range
            raw_start, _separator, raw_end = raw_value.partition("..")

            def parse_boundary(value: str) -> datetime | None:
                if not value.strip():
                    return None
                try:
                    parsed = datetime.fromisoformat(value.strip())
                except ValueError as exc:
                    raise ConfigurationError(
                        "Date-range boundaries must use ISO format, such as 2026-08-10."
                    ) from exc
                return (
                    parsed.replace(tzinfo=ZoneInfo(self.timezone))
                    if parsed.tzinfo is None
                    else parsed.astimezone(ZoneInfo(self.timezone))
                )

            start = parse_boundary(raw_start)
            end = parse_boundary(raw_end)
            if start is None and end is None:
                raise ConfigurationError("Date range needs a start or end boundary.")
            if start is not None and end is not None and end <= start:
                raise ConfigurationError("Date-range end must be after its start.")
            return DateRange(start, end, self.timezone, raw_value)
        if parameter_type in {ParameterType.SIZE, ParameterType.SIZE_RANGE}:
            size = extract_size_constraint(raw_value)
            if size is None:
                raise ConfigurationError(
                    "Size must include a value and unit, such as 'larger than 5 MB'."
                )
            return size
        if parameter_type is ParameterType.EXCLUSION:
            exclusions = extract_exclusions(raw_value)
            if len(exclusions) != 1:
                raise ConfigurationError(
                    "Each exclusion must use a phrase such as 'not from Ahmed'."
                )
            return exclusions[0]
        if parameter_type is ParameterType.RANKING:
            ranking = extract_ranking(raw_value)
            if ranking is None:
                raise ConfigurationError(
                    "Ranking must be a phrase such as 'oldest', 'newest', or 'largest'."
                )
            return ranking
        if parameter_type is ParameterType.THREAD_RESPONSE:
            response = extract_thread_response_state(raw_value)
            if response is not None:
                return response
            normalized = raw_value.casefold().strip()
            if normalized in {"waiting_on_me", "waiting_on_them"}:
                return ThreadResponseState(normalized, raw_value)
            raise ConfigurationError("Response state must be 'waiting on me' or 'waiting on them'.")
        return raw_value

    def _unavailable_reason(
        self, command: CommandSpec, invocation: CommandInvocation | None = None
    ) -> str | None:
        availability = self.registry.availability(
            command.command_id,
            self.runtime_context,
            invocation=invocation,
        )
        reasons = list(availability.reasons) if not availability.available else []
        route_reason = self._route_gate_reason(command.command_id)
        if route_reason:
            reasons.append(route_reason)
        return "; ".join(dict.fromkeys(reasons)) if reasons else None

    @staticmethod
    def _route_gate_reason(command_id: str) -> str | None:
        """Return a TUI-specific capability gate for generated catalog actions."""

        try:
            route = catalog_route(command_id)
        except KeyError:
            return None
        return route.gate_reason if route.is_gate else None

    def _apply_route_availability(self, suggestion: PaletteSuggestion) -> PaletteSuggestion:
        reason = self._route_gate_reason(suggestion.command.command_id)
        if not reason:
            return suggestion
        existing = suggestion.availability.reasons
        availability = CommandAvailability(
            AvailabilityState.UNAVAILABLE,
            tuple(dict.fromkeys((*existing, reason))),
            suggestion.availability.missing_context,
            suggestion.availability.missing_services,
        )
        return replace(suggestion, availability=availability)

    def _submit_suggestion(self, suggestion: PaletteSuggestion) -> None:
        if suggestion.invocation is not None:
            self._submit_invocation(suggestion.invocation, self._interpretation)
        else:
            self._submit_spec(suggestion.command)

    def _submit_spec(self, command: CommandSpec) -> None:
        self._refresh_runtime_context()
        reason = self._unavailable_reason(command)
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}. Choose another command or "
                "restore context."
            )
            return
        parameters: dict[str, object] = {}
        selected = next(
            (
                self.command_context[requirement]
                for requirement in command.context_requirements
                if self.command_context.get(requirement)
            ),
            None,
        )
        allowed = {parameter.name for parameter in command.parameters}
        if selected is not None:
            if "reference" in allowed:
                parameters["reference"] = selected
            if "selection" in allowed:
                parameters["selection"] = (selected,)
        presets = {
            "mail.open_inbox": "inbox",
            "mail.open_sent": "sent",
            "mail.open_drafts": "drafts",
            "mail.open_deleted": "deleted",
            "mail.open_junk": "junk",
            "mail.open_archive": "archive",
        }
        if command.command_id in presets and "folder" in allowed:
            parameters["folder"] = presets[command.command_id]
        query = self.query_one("#palette-input", Input).value.strip()
        if query and "query" in allowed:
            parameters["query"] = query
        invocation = CommandInvocation.from_mapping(
            command.command_id,
            parameters,
            original_text=query or command.name,
            confidence=1.0,
            explanation="Explicit palette selection",
        )
        interpretation = NaturalLanguageResult(
            original=query or command.name,
            normalized=query.casefold() or command.name.casefold(),
            kind=InterpretationKind.COMMAND,
            invocation=invocation,
            candidates=(),
            entities=self._interpretation.entities,
            explanation=f"Explicitly selected {command.name}.",
        )
        self._submit_invocation(invocation, interpretation)

    def _submit_invocation(
        self,
        invocation: CommandInvocation,
        interpretation: NaturalLanguageResult,
    ) -> None:
        self._refresh_runtime_context()
        command = self.registry.get(invocation.command_id)
        plan: CommandPlan = self.engine.plan(invocation, context=self.runtime_context)
        reason = "; ".join(plan.availability.reasons) if not plan.availability.available else ""
        route_reason = self._route_gate_reason(invocation.command_id)
        if route_reason:
            reason = "; ".join(dict.fromkeys(part for part in (reason, route_reason) if part))
        if plan.validation_errors:
            reason = (
                "; ".join((*plan.validation_errors, reason))
                if reason
                else "; ".join(plan.validation_errors)
            )
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}."
            )
            return
        if command.mutates and invocation.parameters and command.command_id != "mail.restore":

            def reviewed(revised: CommandInvocation | None) -> None:
                if revised is not None:
                    self._finish_submission(revised, interpretation)

            self.app.push_screen(
                PaletteParameterReviewScreen(
                    command,
                    invocation,
                    plan.preview,
                    coercer=lambda raw: self._coerce_reviewed_invocation(invocation, raw),
                    confirmation_required=plan.confirmation_required,
                ),
                reviewed,
            )
            return
        self._finish_submission(invocation, interpretation)

    def _finish_submission(
        self,
        invocation: CommandInvocation,
        interpretation: NaturalLanguageResult,
    ) -> None:
        self._refresh_runtime_context()
        command = self.registry.get(invocation.command_id)
        plan: CommandPlan = self.engine.plan(invocation, context=self.runtime_context)
        reason = "; ".join(plan.availability.reasons) if not plan.availability.available else ""
        route_reason = self._route_gate_reason(invocation.command_id)
        if route_reason:
            reason = "; ".join(dict.fromkeys(part for part in (reason, route_reason) if part))
        if plan.validation_errors:
            reason = (
                "; ".join((*plan.validation_errors, reason))
                if reason
                else "; ".join(plan.validation_errors)
            )
        if reason:
            self.query_one("#palette-explanation", Static).update(
                f"{command.name} is unavailable: {reason}."
            )
            return
        # Restore's exact item/destination plan owns the one final confirmation.
        # This submission authorizes opening that preview only, never execution.
        if invocation.command_id == "mail.restore":
            self.dismiss(PaletteSubmission(invocation, interpretation))
            return
        if plan.confirmation_required:
            parameters = self._format_parameters(invocation) or "No parameters"
            scope = (
                f"Server data: {'changes' if command.changes_server else 'unchanged'}\n"
                f"Local data: {'changes' if command.changes_local else 'unchanged'}"
            )
            detail = sanitize_terminal(
                f"{interpretation.explanation}\n\n{parameters}\n\n{scope}\n\n"
                + "\n".join(plan.preview),
                max_length=2_000,
            )

            def confirmed(value: bool | None) -> None:
                if value:
                    self.dismiss(PaletteSubmission(invocation, interpretation, confirmed=True))

            self.app.push_screen(ConfirmationScreen(f"Confirm · {command.name}", detail), confirmed)
            return
        self.dismiss(PaletteSubmission(invocation, interpretation))

    def action_cancel(self) -> None:
        self.dismiss(None)
