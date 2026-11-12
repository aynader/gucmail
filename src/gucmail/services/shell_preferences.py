"""Bounded, read-only header and status formatting shared by settings and the TUI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich.cells import cell_len
from rich.text import Text

from gucmail.security import sanitize_terminal

HEADER_FIELDS = ("account", "storage", "monitoring", "folder")
STATUS_FIELDS = ("activity", "connection", "folder", "monitoring")
HEADER_DEFAULT = HEADER_FIELDS
STATUS_DEFAULT = ("activity",)


def parse_shell_fields(value: object, *, status: bool = False) -> tuple[str, ...]:
    """Validate selected identifiers and order, without silently dropping typos."""
    allowed = STATUS_FIELDS if status else HEADER_FIELDS
    if not isinstance(value, (tuple, list)) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of field identifiers")
    fields = tuple(value)
    if len(fields) != len(set(fields)) or any(v not in allowed for v in fields):
        raise ValueError("choose each field at most once from: " + ", ".join(allowed))
    if status and (not fields or fields[0] != "activity"):
        raise ValueError("activity must remain first so operation results stay visible")
    return fields


def header_variants(
    facts: Mapping[str, str], fields: Sequence[str], *, compactness: str = "adaptive"
) -> tuple[str, ...]:
    values = {key: sanitize_terminal(value, max_length=300) for key, value in facts.items()}
    selected = [key for key in fields if values.get(key)]
    full = " · ".join(values[key] for key in selected)
    if compactness == "detailed":
        return (full,)
    variants = [full]
    short = dict(values)
    if "storage" in selected:
        # A compact label cannot turn unknown/fallback quota facts into a percentage.
        short["storage"] = "Storage details"
    if compactness == "compact":
        variants.insert(0, " · ".join(short[key] for key in selected))
    for omitted in ("folder", "monitoring", "storage"):
        selected = [key for key in selected if key != omitted]
        variants.append(" · ".join(values[key] for key in selected))
    return tuple(dict.fromkeys(variants))


def fit_header(variants: Sequence[str], width: int) -> str:
    if width <= 0 or not variants:
        return ""
    fitting = next((value for value in variants if value and cell_len(value) <= width), None)
    if fitting is not None:
        return fitting
    text = Text(variants[0])
    text.truncate(width, overflow="ellipsis")
    return text.plain


def status_text(activity: str, fields: Sequence[str], facts: Mapping[str, str]) -> str:
    values = {"activity": activity, **facts}
    return " · ".join(
        sanitize_terminal(values.get(key, ""), max_length=1000 if key == "activity" else 160)
        for key in fields
        if values.get(key)
    )
