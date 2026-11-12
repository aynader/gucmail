"""Bounded Exchange item streams for PIM backup; never parse their opaque content."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from gucmail.errors import (
    ConfigurationError,
    MutationResultUnavailableError,
    UnsupportedFeatureError,
)

NATIVE_ITEM_ROLE = "ews_native_item"
NATIVE_ITEM_MEDIA_TYPE = "application/vnd.ms-exchange.item"
NATIVE_ITEM_SCHEMA = 1
MAX_NATIVE_ITEM_BYTES = 100 * 1024 * 1024
_MAX_ID_CHARS = 8192


def validate_item_identifier(value: object, label: str) -> str:
    """Keep opaque Exchange IDs exact, with bounded type and control checks."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > _MAX_ID_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"The Exchange item {label} is missing or invalid.")
    return value


def native_item_limit(limit: int) -> int:
    if type(limit) is not int or limit < 1:
        raise ConfigurationError("The native item byte limit must be a positive integer.")
    return min(limit, MAX_NATIVE_ITEM_BYTES)


def decode_native_item(value: object, *, max_bytes: int) -> bytes:
    """Accept one complete bounded base64 value, without interpreting the decoded data."""
    limit = native_item_limit(max_bytes)
    encoded_limit = 4 * ((limit + 2) // 3)
    # The library materializes the SOAP response; this is a postresponse bound,
    # not a streaming HTTP memory limit. A small whitespace allowance permits
    # line-wrapped XML text without accepting unbounded padding.
    if not isinstance(value, str) or not value or len(value) > encoded_limit * 2 + 1024:
        raise ConfigurationError("Exchange returned an empty or oversized native item stream.")
    normalized = value.translate(str.maketrans("", "", " \t\r\n"))
    if not normalized or len(normalized) > encoded_limit:
        raise ConfigurationError("The native item stream exceeds the configured byte limit.")
    try:
        content = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error):
        raise ConfigurationError(
            "Exchange returned malformed or truncated native item data."
        ) from None
    if (
        not content
        or len(content) > limit
        or base64.b64encode(content).decode("ascii") != normalized
    ):
        raise ConfigurationError("Exchange returned incomplete or invalid native item data.")
    return content


def validate_native_item(content: object, *, max_bytes: int) -> bytes:
    limit = native_item_limit(max_bytes)
    if not isinstance(content, bytes) or not content or len(content) > limit:
        raise ConfigurationError("Native item content is missing or exceeds the configured limit.")
    return content


@dataclass(frozen=True, slots=True)
class NativeItemIdentity:
    item_id: str = field(repr=False)
    change_key: str = field(repr=False)


class NativeItemStreams:
    """One source item or CreateNew destination item per call, using the existing account."""

    def __init__(self, account: Any, *, max_bytes: int) -> None:
        self.account = account
        self.max_bytes = native_item_limit(max_bytes)

    def ensure_supported(self) -> None:
        from exchangelib.version import EXCHANGE_2010_SP1, Build

        build = getattr(getattr(self.account, "version", None), "build", None)
        if not isinstance(build, Build) or build < EXCHANGE_2010_SP1:
            raise UnsupportedFeatureError(
                "Native item backup/restore requires a known Exchange 2010 SP1 or newer version."
            )

    def export_item(self, item: Any) -> bytes:
        from exchangelib.services import ExportItems

        self.ensure_supported()
        item_id = validate_item_identifier(getattr(item, "id", None), "source identifier")
        change_key = validate_item_identifier(getattr(item, "changekey", None), "source change key")
        size = getattr(item, "size", None)
        if type(size) is not int or not 1 <= size <= self.max_bytes:
            raise ConfigurationError(
                "Native item backup requires a known positive item size within the byte limit. "
                "The backup remains incomplete; no item stream was requested."
            )
        values = list(
            islice(
                ExportItems(account=self.account, chunk_size=1).call(
                    items=((item_id, change_key),)
                ),
                2,
            )
        )
        if len(values) != 1:
            raise ConfigurationError("Exchange did not return exactly one native item stream.")
        if isinstance(values[0], BaseException):
            raise values[0]
        return decode_native_item(values[0], max_bytes=self.max_bytes)

    def upload_create(self, folder: Any, content: bytes) -> NativeItemIdentity:
        from exchangelib.errors import ResponseMessageError
        from exchangelib.services import UploadItems

        self.ensure_supported()
        validate_item_identifier(getattr(folder, "id", None), "destination folder identifier")
        validate_item_identifier(
            getattr(folder, "changekey", None), "destination folder change key"
        )
        content = validate_native_item(content, max_bytes=self.max_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        service = UploadItems(account=self.account, chunk_size=1)
        # No historical ItemId is supplied: Update or UpdateOrCreate is impossible.
        try:
            values = list(islice(service.call(items=((folder, (None, False, encoded)),)), 2))
            if len(values) == 1 and isinstance(values[0], BaseException):
                raise values[0]
        except ResponseMessageError:
            # A structured EWS rejection can be translated by the gateway.
            raise
        except Exception:
            raise MutationResultUnavailableError(
                "The native restore response is unavailable; Exchange may have created the item.",
                suggestion=(
                    "Inspect the dedicated restore folder before another attempt. "
                    "No automatic retry was made."
                ),
            ) from None
        try:
            if len(values) != 1 or not isinstance(values[0], tuple) or len(values[0]) != 2:
                raise ValueError("Expected one returned identity")
            item_id = validate_item_identifier(values[0][0], "returned identifier")
            change_key = validate_item_identifier(values[0][1], "returned change key")
        except Exception:
            raise MutationResultUnavailableError(
                "Exchange may have restored the item but its new identity is unavailable.",
                suggestion=(
                    "Inspect the dedicated restore folder before another attempt. "
                    "No automatic retry was made."
                ),
            ) from None
        return NativeItemIdentity(item_id, change_key)
