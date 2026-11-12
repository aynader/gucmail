"""Application-wide constants with no runtime side effects."""

from __future__ import annotations

from gucmail import __version__

APP_NAME = "GUC Mail CLI"
APP_SLUG = "gucmail"
APP_AUTHOR = "GUC Mail CLI"
DEFAULT_EWS_ENDPOINT = "https://mail.guc.edu.eg/EWS/Exchange.asmx"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
MAX_LOCAL_OFFSET = (1 << 63) - 1
DEFAULT_POLL_INTERVAL_SECONDS = 300
SAFE_MINIMUM_POLL_INTERVAL_SECONDS = 60
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
DEFAULT_AUTOSAVE_SECONDS = 30
SCHEMA_VERSION = 16
USER_AGENT = f"gucmail-cli/{__version__}"
