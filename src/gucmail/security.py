"""Security primitives for hostile email and secret-bearing diagnostics."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import socket
from collections.abc import Iterable
from html import unescape
from pathlib import Path, PurePath
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from gucmail.errors import ConfigurationError

# String-style terminal controls (OSC, DCS, APC and PM) may contain printable text before
# their BEL or ST terminator.  Remove the complete sequence before the shorter CSI matcher;
# otherwise a payload such as ``ESC ] 0 ; bad BEL`` can leave the printable suffix ``ad``.
TERMINAL_STRING_RE = re.compile(
    r"(?:\x1B(?:\]|P|_|\^)[^\x07\x1B]*(?:\x07|\x1B\\|$)|"
    r"[\x90\x9d\x9e\x9f][^\x07\x9c]*(?:\x07|\x9c|$))"
)
ANSI_ESCAPE_RE = re.compile(r"(?:\x1B[@-_][0-?]*[ -/]*[@-~]|\x9B[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([\w.+-]+)@([\w.-]+\.[a-z]{2,})(?![\w.-])")
# Header-like secrets often contain spaces (for example ``Authorization: Bearer token``),
# while cookies contain several semicolon-separated values.  Redact the complete logical value
# instead of only the first whitespace-delimited word.
SECRET_HEADER_RE = re.compile(
    r"(?im)(?P<label>\b(?:authorization|proxy-authorization|cookie|set-cookie)\b"
    r"\s*[:=]\s*)[^\r\n]*"
)
SECRET_KV_RE = re.compile(
    r"(?im)(?P<label>\b(?:password|passwd|token|secret|api[-_]?key|x-api-key)\b"
    r"\s*[:=]\s*)[^\r\n,;]*"
)
WEB_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"'\]\)]+")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
    *(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "¹²³"),
}
DANGEROUS_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".cpl",
    ".exe",
    ".hta",
    ".js",
    ".jse",
    ".lnk",
    ".msi",
    ".ps1",
    ".reg",
    ".scr",
    ".vbe",
    ".vbs",
    ".wsf",
}


def sanitize_terminal(value: object, *, max_length: int | None = None) -> str:
    """Strip escape/control characters while retaining normal Unicode and newlines."""

    text = str(value) if value is not None else ""
    text = TERMINAL_STRING_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if max_length is not None and len(text) > max_length:
        return f"{text[: max(0, max_length - 1)]}…"
    return text


def html_to_safe_text(html: str, *, max_length: int = 1_000_000) -> str:
    """Extract readable text without fetching or rendering any remote content."""

    soup = BeautifulSoup(html[:max_length], "html.parser")
    for element in soup(["script", "style", "iframe", "object", "embed", "svg", "head", "title"]):
        element.decompose()
    # Lay text out like a browser would: inline markup (<span>, <sup>, <b>, <a>) stays
    # on the same line; only <br> and block elements break lines. Breaking at every
    # text node turned "6<sup>th</sup> Semester" into three lines.
    for text_node in soup.find_all(string=True):
        if text_node.find_parent("pre") is None:
            text_node.replace_with(_HTML_SPACE_RE.sub(" ", str(text_node)))
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_HTML_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")
    for row in soup.find_all(["li", "tr", "dt", "dd"]):
        row.insert_before("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after("  ")
    lines = [line.strip() for line in unescape(soup.get_text("")).split("\n")]
    text = _HTML_BLANK_RUNS_RE.sub("\n\n", "\n".join(lines)).strip()
    return sanitize_terminal(text, max_length=max_length)


_HTML_SPACE_RE = re.compile(r"[ \t\r\n\f\v]+")
_HTML_BLANK_RUNS_RE = re.compile(r"\n{3,}")
_HTML_BLOCK_TAGS = [
    "p", "div", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "blockquote", "pre", "hr", "section", "article", "header",
    "footer", "dl", "address", "center",
]  # fmt: skip


def safe_filename(name: str, *, fallback: str = "attachment") -> str:
    """Normalize an untrusted attachment name for Windows and POSIX filesystems."""

    candidate = PurePath(name.replace("\\", "/")).name
    candidate = sanitize_terminal(candidate)
    candidate = re.sub(r"[<>:\"/\\|?*]", "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        candidate = fallback
    # Win32 reserves device names even when followed by multiple extensions.
    # Spaces immediately before the first extension are ignored by Win32.
    device_name = candidate.partition(".")[0].rstrip(" ").upper()
    if device_name in WINDOWS_RESERVED:
        candidate = f"_{candidate}"
    if len(candidate) > 180:
        suffix = Path(candidate).suffix[:20]
        candidate = f"{Path(candidate).stem[: 180 - len(suffix)]}{suffix}"
    return candidate


def is_dangerous_attachment(name: str) -> bool:
    return Path(safe_filename(name)).suffix.lower() in DANGEROUS_SUFFIXES


def unique_download_path(
    directory: Path,
    filename: str,
    *,
    overwrite: bool = False,
    reserved: Iterable[Path] = (),
) -> Path:
    """Return a collision-safe path guaranteed to remain below ``directory``."""

    base = directory.resolve()
    candidate = base / safe_filename(filename)
    reserved_names = {os.path.normcase(str(path)) for path in reserved}
    if candidate.parent != base:
        raise ConfigurationError("The attachment filename would escape the download directory.")
    if candidate.is_symlink():
        raise ConfigurationError("Refusing an attachment destination symlink.")
    if overwrite or (
        not candidate.exists()
        and not candidate.is_symlink()
        and os.path.normcase(str(candidate)) not in reserved_names
    ):
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for number in range(1, 10_000):
        numbered = base / f"{stem} ({number}){suffix}"
        if (
            not numbered.exists()
            and not numbered.is_symlink()
            and os.path.normcase(str(numbered)) not in reserved_names
        ):
            return numbered
    raise ConfigurationError("Could not choose a collision-free attachment filename.")


def redact_text(
    value: object,
    *,
    redact_emails: bool = True,
    redact_urls: bool = False,
) -> str:
    text = sanitize_terminal(value)
    text = SECRET_HEADER_RE.sub(lambda match: f"{match.group('label')}<redacted>", text)
    text = SECRET_KV_RE.sub(lambda match: f"{match.group('label')}<redacted>", text)
    if redact_urls:
        text = WEB_URL_RE.sub("<redacted-url>", text)
    if redact_emails:
        text = EMAIL_RE.sub(lambda match: f"<redacted>@{match.group(2)}", text)
    return text


def hmac_signature(secret: bytes, payload: bytes, *, timestamp: str | None = None) -> str:
    signed = payload if timestamp is None else timestamp.encode("utf-8") + b"." + payload
    return "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_email_address(address: str) -> bool:
    if len(address) > 320 or address.count("@") != 1:
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local and domain and "." in domain and " " not in address)


def validate_webhook_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve_dns: bool = False,
) -> str:
    """Reject credentials, non-HTTP schemes, and optional private-network SSRF targets."""

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ConfigurationError("Webhook URLs must use HTTPS.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError("Webhook URL must have a host and may not contain credentials.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} and not allow_private:
        raise ConfigurationError("Private or loopback webhook targets require explicit opt-in.")

    addresses: Iterable[str] = ()
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolve_dns:
            try:
                addresses = {
                    str(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443)
                }
            except OSError as exc:
                raise ConfigurationError(f"Webhook host could not be resolved: {host}") from exc
    if not allow_private:
        for raw in addresses:
            address = ipaddress.ip_address(raw)
            if not address.is_global:
                raise ConfigurationError(
                    "Private, loopback, or link-local webhook targets are blocked."
                )
    return url
