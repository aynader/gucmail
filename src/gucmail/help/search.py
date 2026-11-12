"""Deterministic local full-text, question, typo, and error search for Help."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rapidfuzz.fuzz import WRatio

from gucmail.help.models import HelpRuntimeContext, HelpSearchResult, HelpSkillLevel

if TYPE_CHECKING:
    from gucmail.help.models import HelpPage

_WORD = re.compile(r"[a-z0-9]+")
_MARKDOWN = re.compile(r"[`*_>#|\[\]()]")
_QUESTION_FILLER = frozenset(
    {
        "a",
        "an",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "how",
        "i",
        "is",
        "it",
        "me",
        "my",
        "please",
        "the",
        "to",
        "what",
        "when",
        "where",
        "why",
    }
)
_TOKEN_CORRECTIONS = {
    "backuo": "backup",
    "calender": "calendar",
    "emals": "emails",
    "loadng": "loading",
    "notifcations": "notifications",
    "scheduel": "schedule",
    "serch": "search",
    "webhhok": "webhook",
    "webhok": "webhook",
}
_TYPO_PREFERENCES: dict[str, tuple[str, ...]] = {
    "notifications": ("notifications.windows", "monitoring.overview"),
}
_QUESTION_EXPANSIONS: tuple[tuple[re.Pattern[str], str, tuple[str, ...]], ...] = (
    (
        re.compile(r"\b(?:(?:start|run).*(?:windows|login|sign in)|windows.*start\w*)\b"),
        "startup task scheduler background monitoring",
        ("startup.windows", "monitoring.overview"),
    ),
    (
        re.compile(r"\b(?:mail|messages?).*(?:not|stop|stuck).*(?:load\w*|refresh\w*)\b"),
        "connection timeout stale session infinite loading doctor",
        ("troubleshooting.connection",),
    ),
    (
        re.compile(r"\b(?:schedule|send later|tomorrow email)\b"),
        "scheduled mail delayed outbox undo send",
        ("mail.scheduled", "tutorial.scheduling"),
    ),
    (
        re.compile(r"\b(?:free|clear).*(?:mailbox|space|storage)\b"),
        "cleanup quota large messages reclaimable space",
        ("cleanup.overview", "storage.quota"),
    ),
    (
        re.compile(r"\b(?:restore|recover).*(?:deleted|email|message)\b"),
        "deleted items restore trash",
        ("mail.deleted-items",),
    ),
    (
        re.compile(r"\b(?:preview).*(?:bigger|larger|resize|wide)\b"),
        "layout preview pane divider size",
        ("appearance.layout",),
    ),
    (
        re.compile(
            r"\b(?:(?:pdfs?|attachments?|files?).*(?:search|find)|"
            r"(?:search|find).*(?:pdfs?|attachments?|files?))\b"
        ),
        "attachment search index filename",
        ("search.local", "search.overview"),
    ),
    (
        re.compile(r"\b(?:backup|back up).*(?:all|everything|mailbox)\b"),
        "full backup verify encrypt restore offline",
        ("backup.overview", "tutorial.backup"),
    ),
    (
        re.compile(
            r"\b(?:discord.*(?:mail|notification|webhook)|"
            r"(?:mail|notification|webhook).*discord)\b"
        ),
        "discord notification rule routing privacy webhook",
        ("integrations.discord", "notifications.rules"),
    ),
    (
        re.compile(r"\b(?:purple dot|unread dot|blue dot)\b"),
        "unread indicator message list interface",
        ("mail.reading", "start.interface"),
    ),
    (
        re.compile(r"\b(?:pc|computer).*(?:on|awake|sleep)\b"),
        "does my pc need to stay on monitoring server scheduled",
        ("faq.pc-on",),
    ),
)


@dataclass(frozen=True, slots=True)
class _PreparedQuery:
    phrase: str
    expanded: str
    tokens: frozenset[str]
    core_tokens: frozenset[str]
    preferred_pages: tuple[str, ...]


def normalize_help_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_WORD.findall(normalized))


def _prepare_query(value: str) -> _PreparedQuery:
    phrase = normalize_help_query(value)
    corrected_tokens = tuple(_TOKEN_CORRECTIONS.get(token, token) for token in phrase.split())
    corrected = " ".join(corrected_tokens)
    matches = [item for item in _QUESTION_EXPANSIONS if item[0].search(corrected)]
    expansions = [extra for _, extra, _ in matches]
    question_preferred = tuple(page_id for _, _, page_ids in matches for page_id in page_ids)
    typo_preferred = tuple(
        page_id
        for page_ids in (_TYPO_PREFERENCES.get(token, ()) for token in corrected_tokens)
        for page_id in page_ids
    )
    preferred_pages = question_preferred or typo_preferred
    expanded = " ".join((corrected, *expansions)).strip()
    tokens = frozenset(expanded.split())
    core_tokens = frozenset(token for token in tokens if token not in _QUESTION_FILLER)
    return _PreparedQuery(
        phrase=corrected,
        expanded=expanded,
        tokens=tokens,
        core_tokens=core_tokens or tokens,
        preferred_pages=preferred_pages,
    )


class HelpSearchIndex:
    """A bounded in-memory index designed for a package-resident corpus."""

    def __init__(self, pages: Sequence[HelpPage]) -> None:
        self._pages = tuple(pages)
        self._documents = {
            page.page_id: normalize_help_query(_MARKDOWN.sub(" ", page.searchable_text))
            for page in self._pages
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        context: HelpRuntimeContext | None = None,
    ) -> tuple[HelpSearchResult, ...]:
        self._check_limit(limit)
        prepared = _prepare_query(query)
        if not prepared.phrase:
            return self._contextual_defaults(limit=limit, context=context)

        results: list[HelpSearchResult] = []
        for page in self._pages:
            score, reasons = self._score(page, prepared, context)
            if score >= 34:
                results.append(HelpSearchResult(page=page, score=score, reasons=tuple(reasons)))
        return self._sorted(results, limit)

    def search_error(
        self,
        error_text: str,
        *,
        limit: int = 3,
        context: HelpRuntimeContext | None = None,
    ) -> tuple[HelpSearchResult, ...]:
        """Prefer explicit error signatures, then fall back to normal Help search."""

        self._check_limit(limit)
        prepared = _prepare_query(error_text)
        if not prepared.phrase:
            return self._contextual_defaults(limit=limit, context=context)
        results: list[HelpSearchResult] = []
        for page in self._pages:
            matched: list[str] = []
            best = 0.0
            for pattern in page.error_patterns:
                normalized = normalize_help_query(pattern)
                if normalized and normalized in prepared.phrase:
                    best = max(best, 320.0 + min(40.0, len(normalized)))
                    matched.append(f"error signature: {pattern}")
                else:
                    ratio = float(WRatio(prepared.phrase, normalized)) if normalized else 0.0
                    if ratio >= 78:
                        best = max(best, 190.0 + ratio)
                        matched.append(f"fuzzy error signature {ratio:.0f}")
            lexical, reasons = self._score(page, prepared, context)
            score = best + lexical * (0.35 if best else 1.0)
            if best or lexical >= 48:
                results.append(HelpSearchResult(page, score, tuple((*matched, *reasons))))
        return self._sorted(results, limit)

    def _score(
        self,
        page: HelpPage,
        query: _PreparedQuery,
        context: HelpRuntimeContext | None,
    ) -> tuple[float, list[str]]:
        title = normalize_help_query(page.title)
        page_id = normalize_help_query(page.page_id.replace(".", " "))
        summary = normalize_help_query(page.summary)
        aliases = tuple(normalize_help_query(value) for value in page.aliases)
        keywords = tuple(normalize_help_query(value) for value in page.keywords)
        commands = tuple(
            normalize_help_query(value.replace(".", " ")) for value in page.related_commands
        )
        settings = tuple(
            normalize_help_query(value.replace("_", " ").replace(".", " "))
            for value in page.related_settings
        )
        errors = tuple(normalize_help_query(value) for value in page.error_patterns)
        document = self._documents[page.page_id]
        reasons: list[str] = []
        score = 0.0

        if page.page_id in query.preferred_pages:
            position = query.preferred_pages.index(page.page_id)
            score += max(120.0, 240.0 - position * 35.0)
            reasons.append("question intent")

        # Keep these weights ordered exactly like the documented ranking contract.
        if query.phrase == title:
            score += 320
            reasons.append("exact title")
        if query.phrase in keywords:
            score += 280
            reasons.append("exact keyword")
        if query.phrase in commands:
            score += 250
            reasons.append("exact command")
        if query.phrase in settings:
            score += 235
            reasons.append("exact setting")
        if query.phrase in errors:
            score += 230
            reasons.append("exact error")
        if query.phrase == page_id:
            score += 225
            reasons.append("page identifier")
        if query.phrase in aliases:
            score += 215
            reasons.append("exact alias")

        phrase_fields = (
            (title, 165.0, "title phrase"),
            (summary, 105.0, "summary phrase"),
            (" ".join(keywords), 145.0, "keyword phrase"),
            (" ".join(commands), 135.0, "command phrase"),
            (" ".join(settings), 125.0, "setting phrase"),
            (" ".join(errors), 130.0, "error phrase"),
            (document, 58.0, "article phrase"),
        )
        for field, weight, reason in phrase_fields:
            if query.phrase and query.phrase in field:
                score += weight
                reasons.append(reason)

        title_tokens = frozenset(title.split())
        metadata_tokens = frozenset(
            " ".join((title, summary, *aliases, *keywords, *commands, *settings, *errors)).split()
        )
        document_tokens = frozenset(document.split())
        title_coverage = len(query.core_tokens & title_tokens) / max(1, len(query.core_tokens))
        metadata_coverage = len(query.core_tokens & metadata_tokens) / max(
            1, len(query.core_tokens)
        )
        body_coverage = len(query.core_tokens & document_tokens) / max(1, len(query.core_tokens))
        if title_coverage:
            score += title_coverage * 95
            reasons.append(f"{title_coverage:.0%} title coverage")
        if metadata_coverage:
            score += metadata_coverage * 105
            reasons.append(f"{metadata_coverage:.0%} metadata coverage")
        if body_coverage:
            score += body_coverage * 44
            reasons.append(f"{body_coverage:.0%} article coverage")

        fuzzy_title = float(WRatio(query.phrase, title))
        fuzzy_alias = max((float(WRatio(query.phrase, alias)) for alias in aliases), default=0.0)
        fuzzy_metadata = max(fuzzy_title, fuzzy_alias)
        if fuzzy_metadata >= 58:
            score += (fuzzy_metadata - 50) * 1.7
            reasons.append(f"fuzzy title/alias {fuzzy_metadata:.0f}")
        fuzzy_body = float(WRatio(query.phrase, document))
        if fuzzy_body >= 72:
            score += (fuzzy_body - 65) * 0.7
            reasons.append(f"fuzzy article {fuzzy_body:.0f}")

        if context is not None:
            if context.active_view and context.active_view.casefold() in page.contexts:
                score += 34
                reasons.append("current view")
            if page.supports_platform(context.platform):
                score += 3
            else:
                score -= 25
            views = max(0, int(context.view_counts.get(page.page_id, 0)))
            if views:
                score += min(14.0, math.log2(views + 1) * 2.5)
                reasons.append("frequently viewed")
            skill_bonus = _skill_preference_bonus(page.skill_level, context)
            if skill_bonus:
                score += skill_bonus
                reasons.append("preferred detail level")
        return score, reasons

    def _contextual_defaults(
        self, *, limit: int, context: HelpRuntimeContext | None
    ) -> tuple[HelpSearchResult, ...]:
        defaults: list[HelpSearchResult] = []
        for page in self._pages:
            score = 0.0
            reasons: list[str] = []
            if page.page_id == "start.overview":
                score += 100
                reasons.append("Help home")
            if context is not None and context.active_view:
                if context.active_view.casefold() in page.contexts:
                    score += 80
                    reasons.append("current view")
                if not page.supports_platform(context.platform):
                    score -= 20
            if context is not None:
                skill_bonus = _skill_preference_bonus(page.skill_level, context)
                if skill_bonus:
                    score += skill_bonus
                    reasons.append("preferred detail level")
            if score:
                defaults.append(HelpSearchResult(page, score, tuple(reasons)))
        return self._sorted(defaults, limit)

    @staticmethod
    def _check_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("Help search limit must be between 1 and 100.")

    @staticmethod
    def _sorted(results: list[HelpSearchResult], limit: int) -> tuple[HelpSearchResult, ...]:
        results.sort(key=lambda item: (-item.score, item.page.title.casefold(), item.page.page_id))
        return tuple(results[:limit])


def _skill_preference_bonus(
    page_level: HelpSkillLevel,
    context: HelpRuntimeContext,
) -> float:
    raw = context.settings.get("help.skill_level", HelpSkillLevel.BEGINNER.value)
    try:
        preferred = HelpSkillLevel(str(raw))
    except ValueError:
        preferred = HelpSkillLevel.BEGINNER
    order = {
        HelpSkillLevel.BEGINNER: 0,
        HelpSkillLevel.INTERMEDIATE: 1,
        HelpSkillLevel.ADVANCED: 2,
        HelpSkillLevel.EXPERT: 3,
    }
    distance = abs(order[page_level] - order[preferred])
    return 12.0 if distance == 0 else 4.0 if distance == 1 else -4.0
