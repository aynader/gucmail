"""Bounded, redacted application logging."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from gucmail.security import redact_text


class RedactingFilter(logging.Filter):
    def __init__(self, *, redact_emails: bool = True) -> None:
        super().__init__()
        self.redact_emails = redact_emails

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), redact_emails=self.redact_emails)
        record.args = ()
        return True


def configure_logging(
    log_directory: Path,
    *,
    level: str = "WARNING",
    redact_emails: bool = True,
) -> Path:
    log_directory.mkdir(parents=True, exist_ok=True)
    path = log_directory / "gucmail.log"
    handler = RotatingFileHandler(
        path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.addFilter(RedactingFilter(redact_emails=redact_emails))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("gucmail")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.WARNING))
    root.propagate = False

    # Prevent exchangelib's XML DEBUG logs from leaking mailbox content into our handlers.
    logging.getLogger("exchangelib").setLevel(logging.WARNING)
    return path
