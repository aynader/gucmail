"""Find the signature and quoted earlier conversation in a plain-text message."""

from __future__ import annotations

import re

_WROTE = re.compile(r"^\s*On .{4,200}wrote:\s*$", re.IGNORECASE)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-{2,}\s*$", re.I)
_HEADER = re.compile(r"^\s*From:\s+\S")
_SENT = re.compile(r"^\s*(Sent|Date):\s+\S", re.IGNORECASE)
_UNDERSCORES = re.compile(r"^\s*_{10,}\s*$")
_SIGNATURE_LINES = 15


def _starts_quote(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _WROTE.match(line) or _ORIGINAL.match(line):
        return True
    if _UNDERSCORES.match(line):
        return any(_HEADER.match(item) for item in lines[index + 1 : index + 3])
    if _HEADER.match(line):
        return any(_SENT.match(item) for item in lines[index + 1 : index + 4])
    if line.startswith(">"):
        return all(item.startswith(">") or not item.strip() for item in lines[index:])
    return False


def split_quoted(body: str) -> tuple[str, str]:
    """Return (new text, hidden part). The hidden part is empty when none is found.

    The hidden part is the signature (after a "-- " line) and the quoted history.
    Forwarded messages keep their content, since that is what the sender meant to share.
    """

    head, quoted = _split_history(body)
    lines = head.splitlines()
    for index in range(len(lines) - 1, 0, -1):
        if lines[index].rstrip() == "--" and len(lines) - index <= _SIGNATURE_LINES:
            signature = "\n".join(lines[index:])
            hidden = f"{signature}\n\n{quoted}" if quoted else signature
            return "\n".join(lines[:index]).rstrip(), hidden.rstrip()
    return head, quoted


def _split_history(body: str) -> tuple[str, str]:
    lines = body.splitlines()
    for index in range(1, len(lines)):
        if not _starts_quote(lines, index):
            continue
        if "forwarded" in lines[index].casefold():
            return body, ""
        if all(line.startswith(">") or not line.strip() for line in lines[:index]):
            return body, ""
        head = "\n".join(lines[:index]).rstrip()
        return head, "\n".join(lines[index:])
    return body, ""
