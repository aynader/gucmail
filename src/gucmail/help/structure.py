"""Markdown outlines and a deterministic hierarchy for the embedded Help center."""

from __future__ import annotations

import html
import re
import string
from collections.abc import Sequence

from gucmail.help.models import HelpHeading, HelpPage, HelpTocKind, HelpTocNode

_ATX_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
_SETEXT_HEADING = re.compile(r"^[ \t]{0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INLINE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_REFERENCE_LABEL = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_HTML_TAG = re.compile(r"<[^>]*>")
_INLINE_MARKUP = re.compile(r"[`*_~]")
_REMOVABLE_SLUG_PUNCTUATION = string.punctuation.replace("-", "").replace("_", "")
_SLUG_PUNCTUATION = re.compile(f"[{re.escape(_REMOVABLE_SLUG_PUNCTUATION)}]+")


def markdown_outline(markdown: str) -> tuple[HelpHeading, ...]:
    """Extract headings using anchors compatible with Textual's Markdown widget."""

    raw_headings: list[tuple[int, str]] = []
    previous_line: str | None = None
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines():
        fence = _FENCE.match(line)
        if fence_character is not None:
            if fence:
                marker = fence.group(1)
                if marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
            continue
        if fence:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            previous_line = None
            continue

        atx = _ATX_HEADING.match(line)
        if atx:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", atx.group(2)).strip()
            if title:
                raw_headings.append((len(atx.group(1)), _plain_heading_text(title)))
            previous_line = None
            continue

        setext = _SETEXT_HEADING.match(line)
        if setext and previous_line:
            level = 1 if setext.group(1).startswith("=") else 2
            raw_headings.append((level, _plain_heading_text(previous_line)))
            previous_line = None
            continue
        previous_line = line.strip() or None

    slug_counts: dict[str, int] = {}
    headings: list[HelpHeading] = []
    for level, title in raw_headings:
        base_slug = _heading_slug(title)
        count = slug_counts.get(base_slug, 0)
        slug_counts[base_slug] = count + 1
        anchor = base_slug if count == 0 else f"{base_slug}-{count}"
        headings.append(HelpHeading(level=level, title=title, anchor=anchor))
    return tuple(headings)


def build_help_toc(
    pages: Sequence[HelpPage],
    *,
    platform: str | None = None,
    include_headings: bool = True,
) -> HelpTocNode:
    """Build Help → category → page → heading nodes without UI dependencies."""

    categories: dict[str, list[HelpPage]] = {}
    for page in pages:
        if platform is not None and not page.supports_platform(platform):
            continue
        categories.setdefault(page.category, []).append(page)

    category_nodes: list[HelpTocNode] = []
    for category, category_pages in categories.items():
        page_nodes: list[HelpTocNode] = []
        for page in category_pages:
            heading_nodes: tuple[HelpTocNode, ...] = ()
            if include_headings:
                heading_nodes = tuple(
                    HelpTocNode(
                        node_id=f"heading:{page.page_id}#{heading.anchor}",
                        title=heading.title,
                        kind=HelpTocKind.HEADING,
                        page_id=page.page_id,
                        anchor=heading.anchor,
                    )
                    for heading in markdown_outline(page.body)
                    if heading.level >= 2
                )
            page_nodes.append(
                HelpTocNode(
                    node_id=f"page:{page.page_id}",
                    title=page.title,
                    kind=HelpTocKind.PAGE,
                    page_id=page.page_id,
                    children=heading_nodes,
                )
            )
        category_nodes.append(
            HelpTocNode(
                node_id=f"category:{_heading_slug(category)}",
                title=category,
                kind=HelpTocKind.CATEGORY,
                children=tuple(page_nodes),
            )
        )
    return HelpTocNode(
        node_id="help",
        title="Help",
        kind=HelpTocKind.ROOT,
        children=tuple(category_nodes),
    )


def _plain_heading_text(value: str) -> str:
    value = _INLINE_LINK.sub(r"\1", value)
    value = _REFERENCE_LABEL.sub(r"\1", value)
    value = _HTML_TAG.sub("", value)
    value = _INLINE_MARKUP.sub("", value)
    return html.unescape(value.replace("\\", "")).strip()


def _heading_slug(value: str) -> str:
    value = _SLUG_PUNCTUATION.sub("", value.strip().casefold())
    return re.sub(r"\s", "-", value)
