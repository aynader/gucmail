"""Offline, package-resident Help corpus and navigation APIs."""

from gucmail.help.history import HelpNavigator, HelpStateStore, MemoryHelpStateStore
from gucmail.help.links import HelpLink, extract_markdown_links, parse_help_link
from gucmail.help.models import (
    HelpAvailability,
    HelpCorpusError,
    HelpFeatureKind,
    HelpHeading,
    HelpHomeSections,
    HelpPage,
    HelpRuntimeContext,
    HelpSearchResult,
    HelpSkillLevel,
    HelpTocKind,
    HelpTocNode,
    HelpUserState,
    HelpValidationReport,
    RenderedHelpPage,
)
from gucmail.help.reference import (
    DOCUMENTED_NLP_EXAMPLES,
    DocumentedNlpExample,
    ShortcutHelpRecord,
    shortcut_conflicts,
    shortcut_records,
)
from gucmail.help.repository import HelpRepository
from gucmail.help.search import normalize_help_query
from gucmail.help.structure import build_help_toc, markdown_outline

__all__ = [
    "DOCUMENTED_NLP_EXAMPLES",
    "DocumentedNlpExample",
    "HelpAvailability",
    "HelpCorpusError",
    "HelpFeatureKind",
    "HelpHeading",
    "HelpHomeSections",
    "HelpLink",
    "HelpNavigator",
    "HelpPage",
    "HelpRepository",
    "HelpRuntimeContext",
    "HelpSearchResult",
    "HelpSkillLevel",
    "HelpStateStore",
    "HelpTocKind",
    "HelpTocNode",
    "HelpUserState",
    "HelpValidationReport",
    "MemoryHelpStateStore",
    "RenderedHelpPage",
    "ShortcutHelpRecord",
    "build_help_toc",
    "extract_markdown_links",
    "markdown_outline",
    "normalize_help_query",
    "parse_help_link",
    "shortcut_conflicts",
    "shortcut_records",
]
