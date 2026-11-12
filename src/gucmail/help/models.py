"""Immutable domain models for the offline Help system."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class HelpSkillLevel(StrEnum):
    """Reader experience level used for progressive disclosure."""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    EXPERT = "expert"


class HelpFeatureKind(StrEnum):
    """Whether a page describes local, online, or reference behavior."""

    LOCAL = "local"
    ONLINE = "online"
    CAPABILITY_GATED = "capability_gated"
    REFERENCE = "reference"


class HelpAvailability(StrEnum):
    """Runtime availability rendered separately from static documentation."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class HelpIssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class HelpTocKind(StrEnum):
    """Kinds of entries in the presentation-neutral Help table of contents."""

    ROOT = "root"
    CATEGORY = "category"
    PAGE = "page"
    HEADING = "heading"


def current_platform_name() -> str:
    """Return the stable platform vocabulary used by Help metadata."""

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


@dataclass(frozen=True, slots=True)
class HelpPage:
    """One validated, package-resident Help article."""

    page_id: str
    title: str
    category: str
    summary: str
    body: str
    source_path: str
    legacy_ids: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    related_commands: tuple[str, ...] = ()
    related_settings: tuple[str, ...] = ()
    related_pages: tuple[str, ...] = ()
    skill_level: HelpSkillLevel = HelpSkillLevel.BEGINNER
    platforms: tuple[str, ...] = ("all",)
    contexts: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    feature_kind: HelpFeatureKind = HelpFeatureKind.REFERENCE
    error_patterns: tuple[str, ...] = ()
    nlp_examples: tuple[str, ...] = ()

    @property
    def searchable_text(self) -> str:
        """Return canonical text used by lexical and fuzzy Help search."""

        return "\n".join(
            (
                self.page_id,
                " ".join(self.legacy_ids),
                self.title,
                self.category,
                self.summary,
                " ".join(self.keywords),
                " ".join(self.aliases),
                " ".join(self.related_commands),
                " ".join(self.related_settings),
                " ".join(self.error_patterns),
                " ".join(self.nlp_examples),
                self.body,
            )
        )

    def supports_platform(self, platform: str) -> bool:
        normalized = platform.casefold().strip()
        return "all" in self.platforms or normalized in self.platforms


@dataclass(frozen=True, slots=True)
class ShortcutHelpRecord:
    """One actual or documented binding with its owning screen/control context."""

    shortcut_id: str
    scope: str
    key: str
    action: str
    customizable: bool = False
    setting_id: str | None = None
    command_id: str | None = None
    context: str = ""
    binding_action: str = ""
    priority: bool = False
    context_id: str = ""


@dataclass(frozen=True, slots=True)
class HelpRuntimeContext:
    """Local state used to rank and annotate Help without network access."""

    platform: str = field(default_factory=current_platform_name)
    active_view: str | None = None
    capabilities: Mapping[str, bool | None] = field(default_factory=dict)
    settings: Mapping[str, object] = field(default_factory=dict)
    shortcuts: Mapping[str, str] = field(default_factory=dict)
    shortcut_assignments: tuple[ShortcutHelpRecord, ...] | None = None
    view_counts: Mapping[str, int] = field(default_factory=dict)
    facts: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HelpHeading:
    """A stable Markdown heading target used for jump-to-heading navigation."""

    level: int
    title: str
    anchor: str


@dataclass(frozen=True, slots=True)
class HelpTocNode:
    """One immutable node in the offline Help hierarchy."""

    node_id: str
    title: str
    kind: HelpTocKind
    page_id: str | None = None
    anchor: str | None = None
    children: tuple[HelpTocNode, ...] = ()


@dataclass(frozen=True, slots=True)
class HelpHomeSections:
    """Curated Help-home groups; UIs decide how to lay them out."""

    start_here: tuple[HelpPage, ...]
    contextual: tuple[HelpPage, ...]
    popular: tuple[HelpPage, ...]
    troubleshooting: tuple[HelpPage, ...]
    recent: tuple[HelpPage, ...] = ()
    favorites: tuple[HelpPage, ...] = ()


@dataclass(frozen=True, slots=True)
class HelpSearchResult:
    page: HelpPage
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedHelpPage:
    page: HelpPage
    availability: HelpAvailability
    availability_detail: str
    markdown: str


@dataclass(frozen=True, slots=True)
class HelpValidationIssue:
    severity: HelpIssueSeverity
    code: str
    detail: str
    page_id: str | None = None


@dataclass(frozen=True, slots=True)
class HelpValidationReport:
    issues: tuple[HelpValidationIssue, ...]

    @property
    def errors(self) -> tuple[HelpValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is HelpIssueSeverity.ERROR)

    @property
    def warnings(self) -> tuple[HelpValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is HelpIssueSeverity.WARNING)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        if self.ok:
            return
        details = "; ".join(
            f"{issue.page_id or 'corpus'}:{issue.code}: {issue.detail}" for issue in self.errors
        )
        raise HelpCorpusError(details)


@dataclass(frozen=True, slots=True)
class HelpUserState:
    """Small, non-secret state suitable for SQLite or another local store."""

    recent_page_ids: tuple[str, ...] = ()
    favorite_page_ids: frozenset[str] = frozenset()
    recent_queries: tuple[str, ...] = ()


class HelpCorpusError(ValueError):
    """Raised when package Help resources are missing or invalid."""
