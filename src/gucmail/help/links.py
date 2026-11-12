"""Safe internal-link parsing for inert, offline Help Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
_REFERENCE_LINK = re.compile(r"(?m)^[ \t]{0,3}\[[^\]\r\n]+\]:[ \t]*(?P<target><[^>\r\n]+>|[^\s]+)")
_AUTOLINK = re.compile(r"<(?P<target>[a-z][a-z0-9+.-]{1,31}:[^<>\s]+)>", re.IGNORECASE)
_INTERNAL_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_HELP_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_ALLOWED_INTERNAL_SCHEMES = frozenset({"help", "command", "setting"})


@dataclass(frozen=True, slots=True)
class HelpLink:
    target: str
    scheme: str
    identifier: str


def extract_markdown_links(markdown: str) -> tuple[str, ...]:
    """Extract clickable Markdown destinations without opening anything.

    Inline links, reference definitions, and URI autolinks are all recognized because
    the Markdown renderer accepts each form. Matches nested inside a larger link are
    emitted only once.
    """

    matches: list[tuple[int, int, str]] = []
    covered: list[tuple[int, int]] = []
    for pattern in (_MARKDOWN_LINK, _REFERENCE_LINK):
        for match in pattern.finditer(markdown):
            target = match.group("target")
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            matches.append((match.start(), match.end(), target))
            covered.append((match.start(), match.end()))
    for match in _AUTOLINK.finditer(markdown):
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        matches.append((match.start(), match.end(), match.group("target")))

    targets: list[str] = []
    for _, _, target in sorted(matches, key=lambda item: (item[0], item[1])):
        targets.append(target)
    return tuple(targets)


def parse_help_link(target: str) -> HelpLink:
    """Validate a Help link and classify its inert destination.

    Corpus articles deliberately do not support local filesystem paths, insecure HTTP,
    ``javascript:``, ``data:``, or executable action schemes.
    """

    cleaned = target.strip()
    if not cleaned or any(character.isspace() or ord(character) < 32 for character in cleaned):
        raise ValueError("Help link is empty or contains whitespace or a control character.")
    if cleaned.startswith("#"):
        anchor = unquote(cleaned[1:])
        if not anchor or not _INTERNAL_IDENTIFIER.fullmatch(anchor.casefold()):
            raise ValueError("Help anchor is invalid.")
        return HelpLink(cleaned, "anchor", anchor.casefold())

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.casefold()
    if scheme == "https":
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("External Help URL has an invalid host or port.") from exc
        if (
            not hostname
            or port == 0
            or parsed.username is not None
            or parsed.password is not None
            or "\\" in cleaned
        ):
            raise ValueError("External Help URL must be a credential-free HTTPS URL.")
        return HelpLink(cleaned, scheme, parsed.geturl())
    if scheme not in _ALLOWED_INTERNAL_SCHEMES:
        raise ValueError("Help links must use help:, command:, setting:, a local anchor, or HTTPS.")
    if parsed.query or parsed.fragment or parsed.netloc:
        raise ValueError("Internal Help links may not contain authority, query, or fragment data.")
    identifier = unquote(parsed.path).casefold()
    pattern = _HELP_IDENTIFIER if scheme == "help" else _INTERNAL_IDENTIFIER
    if len(identifier) > 160 or not pattern.fullmatch(identifier):
        raise ValueError("Internal Help link identifier is invalid.")
    return HelpLink(cleaned, scheme, identifier)
