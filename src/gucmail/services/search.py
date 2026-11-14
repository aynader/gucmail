"""Deterministic mailbox query parsing, planning, residual matching, and fuzzy ranking.

No function in this module performs I/O.  The planner only describes which predicates
may safely be sent to an EWS adapter and which must be evaluated over a bounded local
candidate set.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from itertools import islice
from typing import Any, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rapidfuzz import fuzz
from rapidfuzz.distance import OSA

from gucmail.models import Importance, SearchQuery

MAX_QUERY_CHARS = 8_192
MAX_QUERY_TOKENS = 512
MAX_QUERY_DEPTH = 32
DEFAULT_RESIDUAL_LIMIT = 500
HARD_RESIDUAL_LIMIT = 5_000
DEFAULT_RESULT_LIMIT = 50

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w@.+-]+", re.UNICODE)
_SIZE_RE = re.compile(r"(?i)^\s*(\d+(?:\.\d+)?)\s*(b|kb|kib|mb|mib|gb|gib)?\s*$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_URL_RE = re.compile(r"(?i)https?://[^\s<>]+")


class SearchSyntaxError(ValueError):
    """Raised for invalid or pathologically complex deterministic query syntax."""


class MatchMode(StrEnum):
    TERM = "term"
    PHRASE = "phrase"
    PREFIX = "prefix"
    PROXIMITY = "proximity"


class BooleanOperator(StrEnum):
    AND = "AND"
    OR = "OR"


class QueryNode:
    """Marker base for the immutable query AST."""

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class MatchAll(QueryNode):
    def to_dict(self) -> dict[str, Any]:
        return {"type": "all"}


@dataclass(frozen=True, slots=True)
class TermNode(QueryNode):
    value: str
    field: str = "text"
    mode: MatchMode = MatchMode.TERM
    typed_value: object | None = None
    comparison: str = "contains"
    proximity: int | None = None
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        typed = self.typed_value
        if isinstance(typed, datetime):
            typed = typed.isoformat()
        elif isinstance(typed, tuple):
            typed = [item.isoformat() if isinstance(item, datetime) else item for item in typed]
        return {
            "type": "term",
            "field": self.field,
            "value": self.value,
            "mode": self.mode.value,
            "comparison": self.comparison,
            "proximity": self.proximity,
            "typed_value": typed,
        }


@dataclass(frozen=True, slots=True)
class BooleanNode(QueryNode):
    operator: BooleanOperator
    children: tuple[QueryExpression, ...]

    def __post_init__(self) -> None:
        if len(self.children) < 2:
            raise ValueError("A Boolean query node requires at least two children.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.operator.value.lower(),
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class NotNode(QueryNode):
    child: QueryExpression

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not", "child": self.child.to_dict()}


AndNode = BooleanNode
OrNode = BooleanNode
QueryExpression: TypeAlias = MatchAll | TermNode | BooleanNode | NotNode


@dataclass(frozen=True, slots=True)
class ServerFilter:
    field: str
    lookup: str
    value: object


@dataclass(frozen=True, slots=True)
class SearchPlan:
    ast: QueryExpression
    server_predicate: QueryExpression | None
    residual_predicate: QueryExpression | None
    server_filters: tuple[ServerFilter, ...]
    residual_filters: tuple[TermNode, ...]
    required_fields: frozenset[str]
    fallback_reasons: tuple[str, ...]
    residual_candidate_limit: int
    folder_scope: str | None = None
    mailbox_scope: bool = False

    @property
    def uses_residual(self) -> bool:
        return self.residual_predicate is not None

    @property
    def fully_server_side(self) -> bool:
        return self.server_predicate is not None and self.residual_predicate is None

    @property
    def bounded_residual(self) -> bool:
        return self.uses_residual and self.residual_candidate_limit <= HARD_RESIDUAL_LIMIT

    @property
    def execution_summary(self) -> str:
        """Describe where the query runs without overstating Exchange support."""

        if self.mailbox_scope:
            return (
                "Mailbox search merges bounded, server-filtered pages across mail folders: "
                "up to 50 descendant folders and a 200-result window. "
                "Larger folder sets or windows require a narrower folder search. "
                + (
                    f"Local criteria inspect at most {self.residual_candidate_limit} "
                    "globally selected candidates."
                    if self.uses_residual
                    else "Results are merged locally in the requested order."
                )
            )
        server_count = len(self.server_filters)
        if not self.uses_residual:
            if server_count:
                return f"All {server_count} search criterion/criteria run on Exchange."
            return "Exchange supplies the bounded result page without local filtering."
        local = (
            "; ".join(reason.rstrip(".; ") for reason in self.fallback_reasons)
            or "Some criteria require local evaluation"
        )
        if server_count:
            return (
                f"Exchange applies {server_count} criterion/criteria first. {local}. "
                f"At most {self.residual_candidate_limit} candidates are checked locally."
            )
        return f"{local}. At most {self.residual_candidate_limit} candidates are checked locally."


@dataclass(frozen=True, slots=True)
class ResidualEvaluation:
    matches: tuple[object, ...]
    scanned_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class RankedSearchResult:
    item: object
    score: float
    exact: bool
    matched_field: str | None
    explanation: str
    original_index: int


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str = ""
    quoted: bool = False
    prefix: bool = False
    proximity: int | None = None
    raw: str = ""


_FIELD_ALIASES = {
    "scope": "scope",
    "text": "text",
    "from": "sender",
    "sender": "sender",
    "from-domain": "sender_domain",
    "sender-domain": "sender_domain",
    "recipient": "recipient",
    "to": "to",
    "cc": "cc",
    "bcc": "bcc",
    "recipient-domain": "recipient_domain",
    "to-domain": "recipient_domain",
    "subject": "subject",
    "body": "body",
    "after": "after",
    "before": "before",
    "date": "date",
    "has": "has",
    "attachment": "attachment",
    "filename": "attachment",
    "attachment-type": "attachment_type",
    "read": "is_read",
    "unread": "unread",
    "flag": "flagged",
    "flagged": "flagged",
    "importance": "importance",
    "folder": "folder",
    "category": "category",
    "size": "size",
    "min-size": "minimum_size",
    "minimum-size": "minimum_size",
    "max-size": "maximum_size",
    "maximum-size": "maximum_size",
    "id": "internet_message_id",
    "message-id": "internet_message_id",
    "internet-message-id": "internet_message_id",
    "header": "header",
    "url": "url",
    "domain": "domain",
    "person": "person",
    "organization": "organization",
    "company": "company",
    "attendee": "attendee",
    "location": "location",
    "conversation": "conversation",
    "conversation-id": "conversation",
    "similar-to": "similar_to",
    "scheduled": "scheduled",
    "draft": "draft",
    "calendar": "calendar",
    "contact": "contact",
    "task": "task",
    "type": "item_type",
}

_BOOLEAN_FIELDS = {
    "is_read",
    "unread",
    "flagged",
    "scheduled",
    "draft",
    "calendar",
    "contact",
    "task",
}
_SERVER_LOOKUPS = {
    "sender": "sender__email_address__icontains",
    "subject": "subject__icontains",
    "body": "body__icontains",
    "after": "datetime_received__gte",
    "before": "datetime_received__lt",
    "has_attachments": "has_attachments",
    "is_read": "is_read",
    "importance": "importance",
    "category": "categories__contains",
    "minimum_size": "size__gte",
    "maximum_size": "size__lte",
    "internet_message_id": "internet_message_id",
}

_REQUIRED_FIELDS = {
    "text": {
        "subject",
        "body",
        "preview",
        "sender",
        "recipients",
        "attachment_names",
        "folder_path",
    },
    "sender": {"sender"},
    "sender_domain": {"sender"},
    "recipient": {"recipients"},
    "to": {"to"},
    "cc": {"cc"},
    "bcc": {"bcc"},
    "recipient_domain": {"recipients"},
    "subject": {"subject"},
    "body": {"body"},
    "after": {"received_at"},
    "before": {"received_at"},
    "date": {"received_at"},
    "has_attachments": {"has_attachments"},
    "attachment": {"attachment_names"},
    "attachment_type": {"attachment_names"},
    "is_read": {"is_read"},
    "flagged": {"flagged"},
    "importance": {"importance"},
    "folder": {"folder_path"},
    "category": {"categories"},
    "size": {"size"},
    "minimum_size": {"size"},
    "maximum_size": {"size"},
    "internet_message_id": {"internet_message_id"},
    "header": {"headers"},
    "url": {"body", "links"},
    "domain": {"sender", "recipients", "body"},
    "person": {"sender", "recipients"},
    "organization": {"organization"},
    "company": {"company"},
    "attendee": {"attendees"},
    "location": {"location"},
    "conversation": {"conversation_id"},
    "similar_to": {"reference"},
    "scheduled": {"scheduled"},
    "draft": {"draft"},
    "calendar": {"item_type"},
    "contact": {"item_type"},
    "task": {"item_type"},
    "item_type": {"item_type"},
}


def parse_query(
    text: str,
    *,
    timezone: str | tzinfo = UTC,
    now: datetime | None = None,
) -> QueryExpression:
    """Parse stable expert syntax with ``NOT`` > ``AND`` > ``OR`` precedence."""

    if len(text) > MAX_QUERY_CHARS:
        raise SearchSyntaxError(f"Query exceeds the {MAX_QUERY_CHARS}-character limit.")
    tokens = _tokenize(text)
    if not tokens:
        return MatchAll()
    parser = _Parser(tokens, timezone=_timezone(timezone), now=now)
    node = parser.parse()
    if parser.position != len(tokens):
        raise SearchSyntaxError(f"Unexpected token: {tokens[parser.position].raw!r}")
    return node


parse_search_query = parse_query


class _Parser:
    def __init__(self, tokens: Sequence[_Token], *, timezone: tzinfo, now: datetime | None) -> None:
        self.tokens = tokens
        self.position = 0
        self.timezone = timezone
        self.now = _aware(now or datetime.now(timezone), timezone)
        self.depth = 0

    def parse(self) -> QueryExpression:
        node = self._parse_or()
        if self._peek("RPAREN"):
            raise SearchSyntaxError("Unmatched closing parenthesis.")
        return node

    def _parse_or(self) -> QueryExpression:
        children = [self._parse_and()]
        while self._accept("OR"):
            if self._at_end() or self._peek("RPAREN"):
                raise SearchSyntaxError("OR must be followed by a search expression.")
            children.append(self._parse_and())
        return _combine(BooleanOperator.OR, children)

    def _parse_and(self) -> QueryExpression:
        children = [self._parse_not()]
        while not self._at_end() and not self._peek("RPAREN", "OR"):
            self._accept("AND")
            if self._at_end() or self._peek("RPAREN", "OR", "AND"):
                raise SearchSyntaxError("AND must be followed by a search expression.")
            children.append(self._parse_not())
        return _combine(BooleanOperator.AND, children)

    def _parse_not(self) -> QueryExpression:
        if self._accept("NOT"):
            if self._at_end() or self._peek("RPAREN", "AND", "OR"):
                raise SearchSyntaxError("NOT must be followed by a search expression.")
            return NotNode(self._parse_not())
        return self._parse_primary()

    def _parse_primary(self) -> QueryExpression:
        if self._accept("LPAREN"):
            self.depth += 1
            if self.depth > MAX_QUERY_DEPTH:
                raise SearchSyntaxError("Query nesting is too deep.")
            if self._peek("RPAREN"):
                raise SearchSyntaxError("Empty parenthesized query.")
            node = self._parse_or()
            if not self._accept("RPAREN"):
                raise SearchSyntaxError("Unclosed parenthesis.")
            self.depth -= 1
            return node
        if self._at_end() or self._peek("AND", "OR", "RPAREN"):
            token = self.tokens[self.position] if not self._at_end() else None
            raise SearchSyntaxError(
                f"Expected a term, got {token.raw!r}." if token else "Expected a term."
            )
        token = self.tokens[self.position]
        self.position += 1
        return _term(token, timezone=self.timezone, now=self.now)

    def _at_end(self) -> bool:
        return self.position >= len(self.tokens)

    def _peek(self, *kinds: str) -> bool:
        return not self._at_end() and self.tokens[self.position].kind in kinds

    def _accept(self, kind: str) -> bool:
        if self._peek(kind):
            self.position += 1
            return True
        return False


def build_search_plan(
    query: str | SearchQuery | QueryExpression,
    *,
    residual_candidate_limit: int = DEFAULT_RESIDUAL_LIMIT,
    timezone: str | tzinfo = UTC,
    now: datetime | None = None,
) -> SearchPlan:
    """Split a query without ever pushing only one branch of a mixed OR/NOT."""

    if not 1 <= residual_candidate_limit <= HARD_RESIDUAL_LIMIT:
        raise ValueError(f"residual_candidate_limit must be between 1 and {HARD_RESIDUAL_LIMIT}.")
    explicit_folder_scope: str | None = None
    mailbox_scope = False
    if isinstance(query, str):
        ast = parse_query(query, timezone=timezone, now=now)
    elif isinstance(query, SearchQuery):
        _validate_search_query(query)
        # ``SearchQuery.folder`` selects the remote query source.  It is not a
        # display-path substring predicate: standard folders can be localized and
        # callers may supply an opaque local folder reference.
        explicit_folder_scope = query.folder
        mailbox_scope = query.mailbox_scope
        ast = search_query_to_ast(query, timezone=timezone, now=now)
    elif isinstance(query, QueryNode):
        ast = query
    else:
        raise TypeError("query must be text, SearchQuery, or a QueryNode.")

    ast, expression_mailbox_scope = _extract_mailbox_scope(ast)
    mailbox_scope = mailbox_scope or expression_mailbox_scope
    if explicit_folder_scope is None:
        ast, explicit_folder_scope = _extract_folder_scope(ast)
    if mailbox_scope and (
        explicit_folder_scope is not None or any(term.field == "folder" for term in _terms(ast))
    ):
        raise SearchSyntaxError("Mailbox scope cannot be combined with a folder selector.")
    server, residual = _split_plan(ast)
    residual_terms = tuple(_terms(residual)) if residual is not None else ()
    required = frozenset(
        field for term in residual_terms for field in _REQUIRED_FIELDS.get(term.field, {term.field})
    )
    if isinstance(query, SearchQuery) and query.thread_response:
        required = required | {"sender", "conversation_id", "received_at"}
    reasons = tuple(
        dict.fromkeys(
            (
                *_structural_fallback_reasons(residual),
                *(_fallback_reason(term) for term in residual_terms),
                *(
                    (
                        "Conversation response ownership is evaluated over one bounded "
                        "candidate page after the server query.",
                    )
                    if isinstance(query, SearchQuery) and query.thread_response
                    else ()
                ),
                *(
                    (
                        "Frequency ranking is evaluated over one bounded candidate page "
                        "after the server query.",
                    )
                    if isinstance(query, SearchQuery)
                    and query.ranking is not None
                    and query.ranking.field in {"sender_frequency", "thread_message_count"}
                    else ()
                ),
            )
        )
    )
    filters = tuple(_server_filters(server)) if server is not None else ()
    return SearchPlan(
        ast=ast,
        server_predicate=server,
        residual_predicate=residual,
        server_filters=filters,
        residual_filters=residual_terms,
        required_fields=required,
        fallback_reasons=reasons,
        residual_candidate_limit=residual_candidate_limit,
        folder_scope=explicit_folder_scope,
        mailbox_scope=mailbox_scope,
    )


plan_search = build_search_plan


def _validate_search_query(query: SearchQuery) -> None:
    if type(query.mailbox_scope) is not bool:
        raise ValueError("mailbox_scope must be a boolean.")
    if query.mailbox_scope and query.folder is not None:
        raise ValueError("Mailbox scope cannot be combined with a folder selector.")
    if not 0 <= query.fuzzy_threshold <= 100:
        raise ValueError("fuzzy_threshold must be between 0 and 100.")
    if (
        query.after is not None
        and query.before is not None
        and _aware_utc(query.after) >= _aware_utc(query.before)
    ):
        raise ValueError("Search 'after' must be earlier than 'before'.")
    if query.minimum_size is not None and query.minimum_size < 0:
        raise ValueError("minimum_size may not be negative.")
    if query.maximum_size is not None and query.maximum_size < 0:
        raise ValueError("maximum_size may not be negative.")
    if (
        query.minimum_size is not None
        and query.maximum_size is not None
        and query.minimum_size > query.maximum_size
    ):
        raise ValueError("minimum_size may not exceed maximum_size.")
    if query.attachment_name and query.has_attachments is False:
        raise ValueError("attachment_name conflicts with has_attachments=False.")
    if query.folder is not None and not query.folder.strip():
        raise ValueError("folder may not be blank.")
    if query.thread_response not in {None, "waiting_on_me", "waiting_on_them"}:
        raise ValueError("thread_response must be waiting_on_me, waiting_on_them, or omitted.")
    if query.ranking is not None and query.ranking.field == "attachment_type_frequency":
        raise ValueError(
            "Attachment-type frequency ranking requires the local attachment index; "
            "it cannot be evaluated by a live mailbox query."
        )


def search_query_to_ast(
    query: SearchQuery,
    *,
    timezone: str | tzinfo = UTC,
    now: datetime | None = None,
) -> QueryExpression:
    zone = _timezone(timezone)
    nodes: list[QueryExpression] = []
    if query.text:
        nodes.append(TermNode(query.text, raw=query.text))
    pairs: tuple[tuple[str, object | None], ...] = (
        ("sender", query.sender),
        ("recipient", query.recipient),
        ("to", query.to),
        ("cc", query.cc),
        ("bcc", query.bcc),
        ("subject", query.subject),
        ("body", query.body),
        ("after", query.after),
        ("before", query.before),
        ("has_attachments", query.has_attachments),
        ("attachment", query.attachment_name),
        ("is_read", query.is_read),
        ("flagged", query.flagged),
        ("importance", query.importance),
        ("minimum_size", query.minimum_size),
        ("maximum_size", query.maximum_size),
        ("internet_message_id", query.internet_message_id),
    )
    for field, value in pairs:
        if value is None:
            continue
        typed: object = value.value if isinstance(value, Importance) else value
        if isinstance(value, datetime):
            typed = _aware(value, zone)
        nodes.append(
            TermNode(
                value=str(typed).lower() if isinstance(typed, bool) else str(typed),
                field=field,
                typed_value=typed,
                comparison=_default_comparison(field),
            )
        )
    nodes.extend(TermNode(value=category, field="category") for category in query.categories)
    for exclusion in query.exclusions:
        exclusion_typed: object | None = None
        comparison = "contains"
        if exclusion.field == "has_attachments":
            exclusion_typed = _parse_bool(exclusion.value, exclusion.field)
            comparison = "eq"
        nodes.append(
            NotNode(
                TermNode(
                    value=exclusion.value,
                    field=exclusion.field,
                    typed_value=exclusion_typed,
                    comparison=comparison,
                )
            )
        )
    return _combine(BooleanOperator.AND, nodes) if nodes else MatchAll()


def evaluate_residual(item: object, node: QueryExpression | None) -> bool:
    """Evaluate a residual AST against a domain object or mapping without I/O."""

    if node is None or isinstance(node, MatchAll):
        return True
    if isinstance(node, NotNode):
        return not evaluate_residual(item, node.child)
    if isinstance(node, BooleanNode):
        values = (evaluate_residual(item, child) for child in node.children)
        return all(values) if node.operator is BooleanOperator.AND else any(values)
    return _match_term(_document(item), node)


matches_residual = evaluate_residual


def apply_residual(
    candidates: Iterable[object],
    plan_or_node: SearchPlan | QueryExpression | None,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
    max_candidates: int | None = None,
) -> ResidualEvaluation:
    if limit < 0:
        raise ValueError("limit may not be negative.")
    node = plan_or_node.residual_predicate if isinstance(plan_or_node, SearchPlan) else plan_or_node
    cap = max_candidates or (
        plan_or_node.residual_candidate_limit
        if isinstance(plan_or_node, SearchPlan)
        else DEFAULT_RESIDUAL_LIMIT
    )
    if not 1 <= cap <= HARD_RESIDUAL_LIMIT:
        raise ValueError(f"max_candidates must be between 1 and {HARD_RESIDUAL_LIMIT}.")
    matches: list[object] = []
    scanned = 0
    iterator = iter(candidates)
    for candidate in islice(iterator, cap):
        scanned += 1
        if evaluate_residual(candidate, node) and len(matches) < limit:
            matches.append(candidate)
    truncated = next(iterator, _SENTINEL) is not _SENTINEL
    return ResidualEvaluation(tuple(matches), scanned, truncated)


filter_residual = apply_residual


def _typo_score(needle: str, text: str) -> float:
    """Score words that are one or two typos away ("ahmad" for "ahmed", "exma" for "exam").

    Ratio-based scores punish short words: one wrong letter in a five-letter name scores
    80. Here each word of the search may be up to one edit away (two for words of eight
    or more letters, swapped letters count as one), and the score drops 6 per edit.
    """

    words = text.split()
    total = 0
    for token in needle.split():
        allowed = 0 if len(token) < 4 else 1 if len(token) < 8 else 2
        best = min(
            (OSA.distance(token, word, score_cutoff=allowed + 1) for word in words),
            default=allowed + 1,
        )
        if best > allowed:
            return -1.0
        total += best
    return 100.0 - 6 * total


def fuzzy_rerank(
    items: Iterable[object],
    needle: str,
    *,
    threshold: int = 82,
    limit: int = DEFAULT_RESULT_LIMIT,
    max_candidates: int = DEFAULT_RESIDUAL_LIMIT,
    enabled: bool = True,
    fields: Sequence[str] = (
        "subject",
        "sender",
        "folder_path",
        "attachment_names",
        "name",
        "organization",
        "location",
    ),
) -> list[RankedSearchResult]:
    """RapidFuzz reranking with exact matches in a stable leading bucket."""

    if not 0 <= threshold <= 100:
        raise ValueError("threshold must be between 0 and 100.")
    if limit < 0:
        raise ValueError("limit may not be negative.")
    if not 1 <= max_candidates <= HARD_RESIDUAL_LIMIT:
        raise ValueError(f"max_candidates must be between 1 and {HARD_RESIDUAL_LIMIT}.")
    bounded = list(islice(items, max_candidates))
    normalized_needle = normalize_search_text(needle)
    if not enabled or not normalized_needle:
        return [
            RankedSearchResult(item, 0.0, False, None, "Fuzzy ranking is disabled.", index)
            for index, item in enumerate(bounded[:limit])
        ]
    ranked: list[RankedSearchResult] = []
    for index, item in enumerate(bounded):
        document = _document(item)
        best_score = -1.0
        best_field: str | None = None
        best_exact = False
        for field in fields:
            for displayed in _field_strings(document, field):
                normalized = normalize_search_text(displayed)
                if not normalized:
                    continue
                exact = normalized == normalized_needle or normalized_needle in normalized
                score = (
                    100.0
                    if normalized == normalized_needle
                    else (
                        99.0
                        if exact
                        else max(
                            float(fuzz.WRatio(normalized_needle, normalized)),
                            _typo_score(normalized_needle, normalized),
                        )
                    )
                )
                if (exact, score) > (best_exact, best_score):
                    best_exact, best_score, best_field = exact, score, field
        if best_exact or best_score >= threshold:
            explanation = (
                f"Exact {best_field} match."
                if best_exact
                else f"Approximate {best_field} match ({best_score:.0f})."
            )
            ranked.append(
                RankedSearchResult(
                    item=item,
                    score=best_score,
                    exact=best_exact,
                    matched_field=best_field,
                    explanation=explanation,
                    original_index=index,
                )
            )
    ranked.sort(key=lambda result: (not result.exact, -result.score, result.original_index))
    return ranked[:limit]


rerank_fuzzy = fuzzy_rerank


def normalize_search_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", text)).strip()


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text[index] in "()":
            tokens.append(_Token("LPAREN" if text[index] == "(" else "RPAREN", raw=text[index]))
            index += 1
            continue
        if text[index] == "-" and (index + 1 >= len(text) or not text[index + 1].isdigit()):
            tokens.append(_Token("NOT", raw="-"))
            index += 1
            continue
        start = index
        pieces: list[str] = []
        quoted = False
        while index < len(text) and not text[index].isspace() and text[index] not in "()":
            if text[index] not in {'"', "'"}:
                pieces.append(text[index])
                index += 1
                continue
            quote = text[index]
            quoted = True
            index += 1
            while index < len(text) and text[index] != quote:
                if text[index] == "\\" and index + 1 < len(text):
                    index += 1
                pieces.append(text[index])
                index += 1
            if index >= len(text):
                raise SearchSyntaxError("Unterminated quoted phrase.")
            index += 1
        raw = text[start:index]
        value = "".join(pieces)
        proximity: int | None = None
        match = re.search(r"~(\d+)$", value) if quoted else None
        if match:
            proximity = int(match.group(1))
            if proximity > 100:
                raise SearchSyntaxError("Proximity distance may not exceed 100.")
            value = value[: match.start()]
        prefix = not quoted and value.endswith("*")
        if prefix:
            value = value[:-1]
        operator = value.upper()
        kind = operator if not quoted and operator in {"AND", "OR", "NOT"} else "ATOM"
        if not value and kind == "ATOM":
            raise SearchSyntaxError("Empty search term.")
        tokens.append(_Token(kind, value, quoted, prefix, proximity, raw))
        if len(tokens) > MAX_QUERY_TOKENS:
            raise SearchSyntaxError(f"Query exceeds the {MAX_QUERY_TOKENS}-token limit.")
    return tokens


def _term(token: _Token, *, timezone: tzinfo, now: datetime) -> TermNode:
    raw_field: str | None = None
    value = token.value
    if ":" in value:
        candidate, remainder = value.split(":", 1)
        if _FIELD_NAME_RE.fullmatch(candidate):
            raw_field = candidate.casefold()
            if raw_field not in _FIELD_ALIASES:
                raise SearchSyntaxError(f"Unknown search field: {candidate}")
            value = remainder
    field = _FIELD_ALIASES.get(raw_field or "text", "text")
    if raw_field is not None and not value:
        raise SearchSyntaxError(f"Search field {raw_field!r} requires a value.")

    mode = (
        MatchMode.PROXIMITY
        if token.proximity is not None
        else MatchMode.PHRASE
        if token.quoted
        else MatchMode.PREFIX
        if token.prefix
        else MatchMode.TERM
    )
    typed: object | None = None
    comparison = _default_comparison(field)
    if field == "has":
        if value.casefold() not in {"attachment", "attachments"}:
            raise SearchSyntaxError("The has: field currently supports only has:attachment.")
        field, value, typed, comparison = "has_attachments", "true", True, "eq"
    elif field in _BOOLEAN_FIELDS:
        boolean = _parse_bool(value, field)
        if field == "unread":
            field, boolean = "is_read", not boolean
        typed, comparison = boolean, "eq"
    elif field in {"after", "before"}:
        typed = _parse_date(value, timezone=timezone, now=now)
        comparison = "gte" if field == "after" else "lt"
    elif field == "date":
        if ".." in value:
            left, right = value.split("..", 1)
            start = _parse_date(left, timezone=timezone, now=now)
            end = _parse_date(right, timezone=timezone, now=now)
            if start > end:
                raise SearchSyntaxError("Date range start must not be after its end.")
            typed, comparison = (start, end), "range"
        else:
            start = _parse_date(value, timezone=timezone, now=now)
            typed, comparison = (start, start + timedelta(days=1)), "range"
    elif field in {"size", "minimum_size", "maximum_size"}:
        value, typed, comparison = _parse_size_expression(value, field)
    elif field == "importance":
        lowered = value.casefold()
        if lowered not in {item.value for item in Importance}:
            raise SearchSyntaxError("Importance must be low, normal, or high.")
        typed, comparison = lowered, "eq"
    elif field == "internet_message_id":
        typed, comparison = value, "eq"
    return TermNode(value, field, mode, typed, comparison, token.proximity, token.raw)


def _split_plan(node: QueryExpression) -> tuple[QueryExpression | None, QueryExpression | None]:
    if isinstance(node, MatchAll):
        return node, None
    if isinstance(node, TermNode):
        return (node, None) if _server_capable(node) else (None, node)
    if isinstance(node, NotNode):
        # The Exchange adapter currently accepts a flat conjunction of restriction
        # keyword arguments.  Claiming a NOT predicate as server-side and then
        # flattening it would invert its meaning.  Keep it in the bounded residual
        # evaluator until the provider boundary accepts a structured restriction.
        return None, node
    if node.operator is BooleanOperator.OR:
        # The same rule applies to OR: sending its leaves as keyword arguments would
        # silently turn OR into AND.  Correct bounded fallback is preferable to a
        # faster query with false negatives.
        return None, node
    server_children: list[QueryExpression] = []
    residual_children: list[QueryExpression] = []
    for child in node.children:
        server, residual = _split_plan(child)
        if server is not None:
            server_children.append(server)
        if residual is not None:
            residual_children.append(residual)
    return _optional_combine(BooleanOperator.AND, server_children), _optional_combine(
        BooleanOperator.AND, residual_children
    )


def _server_capable(term: TermNode) -> bool:
    return (
        term.field in _SERVER_LOOKUPS
        and term.mode in {MatchMode.TERM, MatchMode.PHRASE}
        and term.comparison != "range"
    )


def _server_filters(node: QueryExpression) -> Iterable[ServerFilter]:
    for term in _terms(node):
        lookup = _SERVER_LOOKUPS.get(term.field)
        if lookup:
            yield ServerFilter(
                term.field, lookup, term.typed_value if term.typed_value is not None else term.value
            )


def _terms(node: QueryExpression | None) -> Iterable[TermNode]:
    if node is None or isinstance(node, MatchAll):
        return
    if isinstance(node, TermNode):
        yield node
    elif isinstance(node, NotNode):
        yield from _terms(node.child)
    else:
        for child in node.children:
            yield from _terms(child)


def _match_term(document: Mapping[str, Any], term: TermNode) -> bool:
    field = term.field
    if field in {"after", "before", "date"}:
        received = _as_datetime(_pick(document, "received_at", "sent_at", "date"))
        if received is None:
            return False
        typed = term.typed_value
        if term.comparison == "range" and isinstance(typed, tuple):
            return _aware_utc(typed[0]) <= _aware_utc(received) < _aware_utc(typed[1])
        if isinstance(typed, datetime):
            return (
                _aware_utc(received) >= _aware_utc(typed)
                if term.comparison == "gte"
                else _aware_utc(received) < _aware_utc(typed)
            )
        return False
    if field in {"size", "minimum_size", "maximum_size"}:
        size = _integer(_pick(document, "size", "size_bytes"))
        typed = term.typed_value
        if term.comparison == "range" and isinstance(typed, tuple):
            return int(typed[0]) <= size <= int(typed[1])
        if not isinstance(typed, int):
            return False
        return {
            "gt": size > typed,
            "gte": size >= typed,
            "lt": size < typed,
            "lte": size <= typed,
            "eq": size == typed,
        }.get(term.comparison, False)
    if field in {"has_attachments", "is_read", "flagged", "scheduled", "draft"}:
        actual_flag = bool(_pick(document, field, field.removeprefix("is_")))
        return actual_flag is bool(term.typed_value)
    if field in {"calendar", "contact", "task"}:
        item_type = str(_pick(document, "item_type", "type", "kind") or "").casefold()
        expected = field
        enabled = bool(term.typed_value)
        return (expected in item_type) is enabled
    if field == "importance":
        importance_value = _pick(document, "importance")
        actual_text = getattr(importance_value, "value", importance_value)
        return str(actual_text or "").casefold() == str(term.typed_value).casefold()
    values = _values_for_term(document, term)
    return any(_text_match(value, term) for value in values)


def _values_for_term(document: Mapping[str, Any], term: TermNode) -> list[str]:
    field = term.field
    names: tuple[str, ...]
    if field == "text":
        names = (
            "subject",
            "body",
            "body_text",
            "preview",
            "sender",
            "to",
            "cc",
            "bcc",
            "recipients",
            "attachment_names",
            "folder_path",
            "categories",
        )
    elif field == "sender_domain":
        return [_domain(value) for value in _field_strings(document, "sender")]
    elif field == "recipient_domain":
        return [
            _domain(value)
            for name in ("recipients", "to", "cc", "bcc")
            for value in _field_strings(document, name)
        ]
    elif field == "recipient":
        names = ("recipients", "to", "cc", "bcc")
    elif field == "attachment_type":
        return [value.rsplit(".", 1)[-1] for value in _field_strings(document, "attachment_names")]
    elif field == "attachment":
        names = ("attachment_names", "attachments")
    elif field == "folder":
        names = ("folder_path", "folder")
    elif field == "category":
        names = ("categories",)
    elif field == "header":
        names = ("headers", "raw_headers")
    elif field == "url":
        return [
            url
            for name in ("body", "body_text", "links")
            for value in _field_strings(document, name)
            for url in _URL_RE.findall(value)
        ]
    elif field == "domain":
        names = ("sender", "recipients", "to", "cc", "bcc", "body", "body_text")
    elif field == "person":
        names = ("sender", "recipients", "to", "cc", "bcc", "name", "display_name")
    elif field == "conversation":
        names = ("conversation_id", "conversation_topic")
    elif field == "similar_to":
        names = ("reference", "id")
    elif field == "internet_message_id":
        names = ("internet_message_id", "message_id")
    else:
        names = (field,)
    return [value for name in names for value in _field_strings(document, name)]


def _text_match(displayed: str, term: TermNode) -> bool:
    haystack = normalize_search_text(displayed)
    needle = normalize_search_text(term.value)
    if not needle:
        return False
    if term.mode is MatchMode.PREFIX:
        return any(token.startswith(needle) for token in haystack.split())
    if term.mode is MatchMode.PROXIMITY:
        words = needle.split()
        positions = [
            [index for index, token in enumerate(haystack.split()) if token == word]
            for word in words
        ]
        if any(not group for group in positions):
            return False
        distance = term.proximity or 1
        return any(
            max(combo) - min(combo) <= distance + len(words) - 1
            for combo in _product_positions(positions)
        )
    return needle in haystack


def _document(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        raw = dict(value)
    elif is_dataclass(value) and not isinstance(value, type):
        raw = asdict(value)
    else:
        raw = {
            name: getattr(value, name) for name in _KNOWN_DOCUMENT_FIELDS if hasattr(value, name)
        }
    summary = raw.get("summary")
    if isinstance(summary, Mapping):
        merged = dict(summary)
        merged.update(raw)
        return merged
    return raw


_KNOWN_DOCUMENT_FIELDS = frozenset(
    field for fields in _REQUIRED_FIELDS.values() for field in fields
) | {
    "summary",
    "reference",
    "attachments",
    "raw_headers",
    "body_text",
    "folder",
    "size",
    "type",
    "kind",
}


def _field_strings(document: Mapping[str, Any], field: str) -> list[str]:
    value = document.get(field)
    if value is None:
        aliases = {"folder_path": "folder", "attachment_names": "attachments"}
        value = document.get(aliases.get(field, ""))
    return _string_values(value)


def _string_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        if "email" in value or "address" in value:
            return [str(value.get("email") or value.get("address") or value.get("name") or "")]
        if "name" in value or "filename" in value:
            return [str(value.get("name") or value.get("filename") or "")]
        return [f"{key}: {item}" for key, item in value.items()]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, memoryview)):
        return [text for item in islice(value, 256) for text in _string_values(item)]
    email = getattr(value, "email", None)
    if email:
        return [str(email)]
    name = getattr(value, "name", None)
    if name:
        return [str(name)]
    return [str(value)]


def _fallback_reason(term: TermNode) -> str:
    descriptions = {
        "text": "Free text is evaluated by the local index or bounded residual search.",
        "recipient": "Recipient fields are not reliably searchable through EWS.",
        "to": "To recipients require bounded residual evaluation.",
        "cc": "Cc recipients require bounded residual evaluation.",
        "bcc": "Bcc recipients require bounded residual evaluation.",
        "attachment": "Attachment filenames require local metadata.",
        "folder": "Folder is an execution scope and is verified locally.",
        "flagged": "Flag state requires an optional extended property.",
    }
    return descriptions.get(term.field, f"{term.field} requires bounded local evaluation.")


def _structural_fallback_reasons(node: QueryExpression | None) -> tuple[str, ...]:
    reasons: list[str] = []

    def visit(current: QueryExpression | None) -> None:
        if current is None or isinstance(current, (MatchAll, TermNode)):
            return
        if isinstance(current, NotNode):
            reasons.append(
                "NOT expressions use bounded local evaluation because the EWS gateway "
                "does not yet accept structured negation."
            )
            visit(current.child)
            return
        if current.operator is BooleanOperator.OR:
            reasons.append(
                "OR expressions use bounded local evaluation because flattening them "
                "into Exchange keyword filters would change their meaning."
            )
        for child in current.children:
            visit(child)

    visit(node)
    return tuple(dict.fromkeys(reasons))


def _parse_bool(value: str, field: str) -> bool:
    lowered = value.casefold()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    raise SearchSyntaxError(f"{field} expects true or false.")


def _parse_date(value: str, *, timezone: tzinfo, now: datetime) -> datetime:
    lowered = value.strip().casefold()
    if lowered == "today":
        return datetime.combine(now.date(), time.min, timezone)
    if lowered == "yesterday":
        return datetime.combine(now.date() - timedelta(days=1), time.min, timezone)
    match = re.fullmatch(r"(\d+)d(?:ays?)?(?:-ago)?", lowered)
    if match:
        return datetime.combine(
            now.date() - timedelta(days=int(match.group(1))), time.min, timezone
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as exc:
            raise SearchSyntaxError(f"Invalid date: {value}") from exc
        parsed = datetime.combine(parsed_date, time.min, timezone)
    return _aware(parsed, timezone)


def _parse_size_expression(value: str, field: str) -> tuple[str, object, str]:
    comparison = "gte" if field == "minimum_size" else "lte" if field == "maximum_size" else "eq"
    if ".." in value:
        left, right = value.split("..", 1)
        minimum, maximum = _parse_size(left), _parse_size(right)
        if minimum > maximum:
            raise SearchSyntaxError("Size range start must not exceed its end.")
        return value, (minimum, maximum), "range"
    for marker, operation in ((">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt")):
        if value.startswith(marker):
            return value[len(marker) :], _parse_size(value[len(marker) :]), operation
    return value, _parse_size(value), comparison


def _parse_size(value: str) -> int:
    match = _SIZE_RE.fullmatch(value)
    if not match:
        raise SearchSyntaxError(f"Invalid message size: {value}")
    number = float(match.group(1))
    unit = (match.group(2) or "b").casefold()
    multiplier = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1_000_000,
        "mib": 1_048_576,
        "gb": 1_000_000_000,
        "gib": 1_073_741_824,
    }[unit]
    result = int(number * multiplier)
    if result < 0 or result > 2**63 - 1:
        raise SearchSyntaxError("Message size is outside the supported range.")
    return result


def _timezone(value: str | tzinfo) -> tzinfo:
    if isinstance(value, str):
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise SearchSyntaxError(f"Unknown time zone: {value}") from exc
    return value


def _aware(value: datetime, timezone: tzinfo) -> datetime:
    return value.replace(tzinfo=timezone) if value.tzinfo is None else value.astimezone(timezone)


def _aware_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime value.")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _default_comparison(field: str) -> str:
    return (
        "eq"
        if field in _BOOLEAN_FIELDS | {"has_attachments", "importance", "internet_message_id"}
        else "contains"
    )


def _combine(operator: BooleanOperator, children: Sequence[QueryExpression]) -> QueryExpression:
    flattened: list[QueryExpression] = []
    for child in children:
        if isinstance(child, BooleanNode) and child.operator is operator:
            flattened.extend(child.children)
        else:
            flattened.append(child)
    if not flattened:
        return MatchAll()
    return flattened[0] if len(flattened) == 1 else BooleanNode(operator, tuple(flattened))


def _optional_combine(
    operator: BooleanOperator, children: Sequence[QueryExpression]
) -> QueryExpression | None:
    return _combine(operator, children) if children else None


def _extract_folder_scope(node: QueryExpression) -> tuple[QueryExpression, str | None]:
    """Extract one positive top-level folder selector from expert syntax.

    A folder selector chooses the EWS folder object.  Keeping the same token as a
    local path predicate is both redundant and incorrect for localized names or
    opaque references.  Selectors under OR or NOT remain ordinary residual terms.
    """

    if isinstance(node, TermNode) and node.field == "folder":
        return MatchAll(), node.value
    if not isinstance(node, BooleanNode) or node.operator is not BooleanOperator.AND:
        return node, None
    selectors = [
        child for child in node.children if isinstance(child, TermNode) and child.field == "folder"
    ]
    if len(selectors) != 1:
        return node, None
    remaining = tuple(child for child in node.children if child is not selectors[0])
    return _combine(BooleanOperator.AND, remaining), selectors[0].value


def _extract_mailbox_scope(node: QueryExpression) -> tuple[QueryExpression, bool]:
    """A mailbox selector changes the query source; it is never a content predicate."""

    terms = [term for term in _terms(node) if term.field == "scope"]
    if not terms:
        return node, False
    if any(term.value.casefold() != "mailbox" for term in terms):
        raise SearchSyntaxError("The only explicit search scope is scope:mailbox.")
    if isinstance(node, TermNode):
        return MatchAll(), True
    if not isinstance(node, BooleanNode) or node.operator is not BooleanOperator.AND:
        raise SearchSyntaxError("Mailbox scope must be a positive top-level AND selector.")
    selected = [
        child for child in node.children if isinstance(child, TermNode) and child.field == "scope"
    ]
    if len(selected) != len(terms):
        raise SearchSyntaxError("Mailbox scope cannot appear under OR or NOT.")
    return _combine(
        BooleanOperator.AND, tuple(child for child in node.children if child not in selected)
    ), True


def _pick(document: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if name in document and document[name] is not None:
            return document[name]
    return None


def _integer(value: object) -> int:
    try:
        return int(str(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _domain(value: str) -> str:
    return value.rsplit("@", 1)[-1] if "@" in value else value


def _product_positions(groups: Sequence[Sequence[int]]) -> Iterable[tuple[int, ...]]:
    if not groups:
        return
    partial: list[tuple[int, ...]] = [()]
    for group in groups:
        partial = [(*prefix, position) for prefix in partial for position in group][:10_000]
    yield from partial


_SENTINEL = object()


__all__ = [
    "DEFAULT_RESIDUAL_LIMIT",
    "HARD_RESIDUAL_LIMIT",
    "AndNode",
    "BooleanNode",
    "BooleanOperator",
    "MatchAll",
    "MatchMode",
    "NotNode",
    "OrNode",
    "QueryExpression",
    "QueryNode",
    "RankedSearchResult",
    "ResidualEvaluation",
    "SearchPlan",
    "SearchSyntaxError",
    "ServerFilter",
    "TermNode",
    "apply_residual",
    "build_search_plan",
    "evaluate_residual",
    "filter_residual",
    "fuzzy_rerank",
    "matches_residual",
    "normalize_search_text",
    "parse_query",
    "parse_search_query",
    "plan_search",
    "rerank_fuzzy",
    "search_query_to_ast",
]
