"""Pure, bounded conversion from exchangelib objects to domain models.

This module deliberately uses structural access instead of importing exchangelib.  Besides
keeping package import side-effect free, this makes the conversion rules straightforward to
exercise with small synthetic objects in unit tests.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from itertools import islice
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gucmail.models import (
    Address,
    AttachmentInfo,
    CalendarEvent,
    ContactInfo,
    FolderInfo,
    Importance,
    ItemSource,
    MessageDetail,
    MessageSummary,
    Sensitivity,
    TaskInfo,
)
from gucmail.security import html_to_safe_text, is_dangerous_attachment, sanitize_terminal
from gucmail.services.calendar_history import (
    CalendarOccurrenceInfo,
    CalendarSeriesHistory,
    history_limit,
)
from gucmail.services.exchange_values import normalize_availability
from gucmail.services.recurrence import normalize_recurrence_mapping

MAX_LIST_RECIPIENTS = 500
MAX_LIST_ATTACHMENTS = 500
MAX_LIST_CATEGORIES = 200
MAX_HEADERS = 1_000
MAX_LINKS = 200
MAX_BODY_CHARS = 2_000_000
MAX_PREVIEW_CHARS = 500

_LINK_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_REFERENCE_RE = re.compile(r"[^\s,]+")
_REFERENCE_PREFIXES = {
    "attachment": "att",
    "calendar": "evt",
    "contact": "con",
    "contact_group": "grp",
    "draft": "drf",
    "event": "evt",
    "folder": "fld",
    "message": "msg",
    "task": "tsk",
}

T = TypeVar("T")
ReferenceFactory = Callable[[str, str], str]


def domain_datetime(value: datetime | None) -> datetime | None:
    """Strip provider subclasses, whose timezone operations are incompatible.

    EWSDateTime rejects datetime.UTC used by persistence, scheduling and UI code.
    Retain wall time, timezone rules and fold using standard-library objects.
    """
    if value is None:
        return None
    zone = value.tzinfo
    if isinstance(zone, ZoneInfo):
        zone = ZoneInfo(zone.key)
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=zone,
        fold=value.fold,
    )


def domain_date(value: date | None) -> date | None:
    return date(value.year, value.month, value.day) if value is not None else None


def _get(value: object, name: str, default: T | None = None) -> Any | T | None:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded(values: object | None, maximum: int) -> tuple[Any, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)):
        return (values,)
    if not isinstance(values, Iterable):
        return ()
    result: list[Any] = []
    for value in values:
        if len(result) >= maximum:
            break
        result.append(value)
    return tuple(result)


def _id(value: object) -> str | None:
    raw = _get(value, "id")
    if raw is None:
        nested = _get(value, "_id")
        raw = _get(nested, "id") if nested is not None else None
    return str(raw) if raw else None


def _changekey(value: object) -> str | None:
    raw = _get(value, "changekey")
    if raw is None:
        nested = _get(value, "_id")
        raw = _get(nested, "changekey") if nested is not None else None
    return str(raw) if raw else None


def make_local_reference(
    kind: str,
    remote_id: str,
    *,
    account_key: str | None = None,
) -> str:
    """Return a short opaque reference without exposing the long EWS identifier."""

    normalized = kind.casefold().strip()
    prefix = _REFERENCE_PREFIXES.get(normalized, re.sub(r"[^a-z0-9]", "", normalized)[:3])
    prefix = prefix or "ews"
    namespace = ""
    if account_key is not None:
        normalized_account = account_key.casefold().strip()
        if normalized_account:
            namespace = f"account\0{normalized_account}\0"
    digest = hashlib.blake2s(
        f"gucmail\0{namespace}{normalized}\0{remote_id}".encode(), digest_size=10
    ).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{token}"


def remote_identity(
    value: object,
    *,
    kind: str,
    fallback: str = "unknown",
    account_key: str | None = None,
) -> tuple[str, str]:
    remote_id = _id(value) or fallback
    return make_local_reference(kind, remote_id, account_key=account_key), remote_id


def map_address(value: object | None) -> Address | None:
    if value is None:
        return None
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        email = value.strip()
        return Address(email=email) if email else None
    raw_email = _get(value, "email_address") or _get(value, "email") or _get(value, "address")
    if not raw_email:
        mailbox = _get(value, "mailbox")
        if mailbox is not None and mailbox is not value:
            return map_address(mailbox)
        return None
    name = _get(value, "name") or _get(value, "display_name")
    mailbox_type = _get(value, "mailbox_type") or _get(value, "routing_type")
    return Address(
        email=sanitize_terminal(raw_email, max_length=320).strip(),
        name=sanitize_terminal(name, max_length=500).strip() if name else None,
        mailbox_type=str(mailbox_type) if mailbox_type else None,
    )


def map_addresses(
    values: Iterable[object] | None, *, maximum: int = MAX_LIST_RECIPIENTS
) -> tuple[Address, ...]:
    result: list[Address] = []
    seen: set[str] = set()
    for value in values or ():
        address = map_address(value)
        if address is None or not address.email:
            continue
        key = address.email.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(address)
        if len(result) >= maximum:
            break
    return tuple(result)


def _importance(value: object | None) -> Importance:
    normalized = str(value or "normal").casefold().replace("_", "")
    if normalized == "high":
        return Importance.HIGH
    if normalized == "low":
        return Importance.LOW
    return Importance.NORMAL


def _sensitivity(value: object | None) -> Sensitivity:
    normalized = str(value or "normal").casefold()
    try:
        return Sensitivity(normalized)
    except ValueError:
        return Sensitivity.NORMAL


def _categories(value: object | None) -> tuple[str, ...]:
    if not value or isinstance(value, (str, bytes, bytearray)):
        return (sanitize_terminal(value, max_length=255),) if value else ()
    return tuple(
        sanitize_terminal(item, max_length=255)
        for item in _bounded(value, MAX_LIST_CATEGORIES)
        if str(item).strip()
    )


def _conversation_id(value: object) -> str | None:
    conversation = _get(value, "conversation_id")
    if conversation is None:
        return None
    raw = _get(conversation, "id", conversation)
    return str(raw) if raw else None


def _flagged(value: object) -> bool:
    for name in ("gucmail_flag_status", "flag_status", "flag"):
        raw = _get(value, name)
        if raw is not None:
            if isinstance(raw, bool):
                return raw
            with_value = str(raw).casefold()
            return raw == 2 or with_value in {"2", "flagged", "active", "true"}
    return False


def map_folder(
    folder: object,
    *,
    reference: str | None = None,
    parent_reference: str | None = None,
    path: str | None = None,
) -> FolderInfo:
    remote_id = _id(folder)
    reference = reference or make_local_reference(
        "folder", remote_id or str(_get(folder, "name", ""))
    )
    parent_id = _get(_get(folder, "parent_folder_id"), "id")
    if parent_reference is None and parent_id:
        parent_reference = make_local_reference("folder", str(parent_id))
    name = sanitize_terminal(_get(folder, "name", "Unnamed folder"), max_length=1_024)
    if path is None:
        absolute = _get(folder, "absolute")
        path = sanitize_terminal(absolute, max_length=8_192) if absolute else name
    distinguished = _get(folder, "DISTINGUISHED_FOLDER_ID") or _get(folder, "distinguished_role")
    return FolderInfo(
        reference=reference,
        name=name,
        path=path,
        parent_reference=parent_reference,
        total_count=_get(folder, "total_count"),
        unread_count=_get(folder, "unread_count"),
        child_count=_get(folder, "child_folder_count", _get(folder, "child_count")),
        folder_class=_get(folder, "folder_class"),
        distinguished_role=str(distinguished) if distinguished else None,
        ews_id=remote_id,
        change_key=_changekey(folder),
    )


def _attachment_remote_id(attachment: object) -> str | None:
    attachment_id = _get(attachment, "attachment_id")
    return (
        str(_get(attachment_id, "id"))
        if attachment_id is not None and _get(attachment_id, "id")
        else None
    )


def is_item_attachment(value: object) -> bool:
    return (
        type(value).__name__.casefold() == "itemattachment"
        or str(_get(value, "ELEMENT_NAME", "")).casefold() == "itemattachment"
        or bool(_get(value, "is_item_attachment", False))
    )


def map_attachment(
    attachment: object,
    *,
    reference: str | None = None,
    parent_remote_id: str | None = None,
    index: int = 0,
) -> AttachmentInfo:
    name = sanitize_terminal(_get(attachment, "name", "attachment"), max_length=1_024)
    remote_id = _attachment_remote_id(attachment)
    reference = reference or make_local_reference(
        "attachment",
        remote_id or f"{parent_remote_id or 'unknown'}:{index}:{name}:{_get(attachment, 'size')}",
    )
    return AttachmentInfo(
        reference=reference,
        name=name,
        size=_get(attachment, "size"),
        content_type=_get(attachment, "content_type"),
        is_inline=bool(_get(attachment, "is_inline", False)),
        is_item_attachment=is_item_attachment(attachment),
        content_id=_get(attachment, "content_id"),
        dangerous=is_dangerous_attachment(name),
    )


def map_attachments(
    values: Iterable[object] | None,
    *,
    parent_remote_id: str | None = None,
    maximum: int = MAX_LIST_ATTACHMENTS,
) -> tuple[AttachmentInfo, ...]:
    return tuple(
        map_attachment(value, parent_remote_id=parent_remote_id, index=index)
        for index, value in enumerate(_bounded(values, maximum))
    )


def map_message_summary(
    message: object,
    *,
    reference: str | None = None,
    folder_path: str | None = None,
    source: ItemSource = ItemSource.LIVE,
) -> MessageSummary:
    remote_id = _id(message)
    reference = reference or make_local_reference("message", remote_id or "unknown")
    # Summary mapping must remain metadata-only.  In particular, do not probe
    # ``text_body`` here: list queries deliberately omit body fields, and a
    # provider object may lazily retrieve or reject access to an unprojected
    # value.  A provider may supply a dedicated bounded preview field, but full
    # body-derived previews belong to the explicit detail path.
    preview = _get(message, "preview")
    if preview:
        preview = sanitize_terminal(preview, max_length=MAX_PREVIEW_CHARS)
    sender = map_address(_get(message, "sender") or _get(message, "author"))
    return MessageSummary(
        reference=reference,
        subject=sanitize_terminal(_get(message, "subject", "") or "", max_length=2_048),
        sender=sender,
        received_at=domain_datetime(
            _get(message, "datetime_received") or _get(message, "received_at")
        ),
        sent_at=domain_datetime(_get(message, "datetime_sent") or _get(message, "sent_at")),
        is_read=bool(_get(message, "is_read", False)),
        has_attachments=bool(_get(message, "has_attachments", False)),
        importance=_importance(_get(message, "importance")),
        flagged=_flagged(message),
        categories=_categories(_get(message, "categories")),
        size=_get(message, "size"),
        folder_path=folder_path or _get(message, "folder_path"),
        conversation_id=_conversation_id(message),
        preview=preview,
        source=source,
    )


def body_parts(message: object) -> tuple[str, str | None]:
    body = _get(message, "body")
    text_body = _get(message, "text_body")
    if body is None:
        return (sanitize_terminal(text_body, max_length=MAX_BODY_CHARS) if text_body else "", None)
    raw = str(body)[:MAX_BODY_CHARS]
    body_type = str(_get(body, "body_type", "Text")).casefold()
    if body_type == "html" or type(body).__name__.casefold() == "htmlbody":
        plain = (
            sanitize_terminal(text_body, max_length=MAX_BODY_CHARS)
            if text_body
            else html_to_safe_text(raw)
        )
        return plain, raw
    return sanitize_terminal(raw, max_length=MAX_BODY_CHARS), None


def _headers(values: object | None) -> dict[str, str]:
    if not values:
        return {}
    if isinstance(values, Mapping):
        return {
            sanitize_terminal(key, max_length=256): sanitize_terminal(item, max_length=32_768)
            for key, item in tuple(values.items())[:MAX_HEADERS]
        }
    result: dict[str, str] = {}
    for header in _bounded(values, MAX_HEADERS):
        name = _get(header, "name")
        if not name:
            continue
        key = sanitize_terminal(name, max_length=256)
        item = sanitize_terminal(_get(header, "value", ""), max_length=32_768)
        result[key] = f"{result[key]}\n{item}" if key in result else item
    return result


def _references(value: object | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(_REFERENCE_RE.findall(value)[:100])
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value[:100] if item)
    return (str(value),)


def _links(*values: str | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for match in _LINK_RE.findall(value):
            link = match.rstrip('.,;:!?)"]}')
            if link in seen:
                continue
            seen.add(link)
            result.append(link)
            if len(result) >= MAX_LINKS:
                return tuple(result)
    return tuple(result)


def _extended_properties(message: object) -> dict[str, Any]:
    supplied = _get(message, "extended_properties")
    result = dict(supplied) if isinstance(supplied, Mapping) else {}
    flag = _get(message, "gucmail_flag_status")
    if flag is not None:
        result["flag_status"] = flag
    return result


def map_message_detail(
    message: object,
    *,
    reference: str | None = None,
    folder_path: str | None = None,
    source: ItemSource = ItemSource.LIVE,
) -> MessageDetail:
    remote_id = _id(message)
    summary = map_message_summary(
        message, reference=reference, folder_path=folder_path, source=source
    )
    text, html = body_parts(message)
    attachments = map_attachments(_get(message, "attachments"), parent_remote_id=remote_id)
    return MessageDetail(
        summary=summary,
        to=map_addresses(_get(message, "to_recipients") or _get(message, "to")),
        cc=map_addresses(_get(message, "cc_recipients") or _get(message, "cc")),
        bcc=map_addresses(_get(message, "bcc_recipients") or _get(message, "bcc")),
        reply_to=map_addresses(_get(message, "reply_to")),
        body_text=text,
        body_html=html,
        sensitivity=_sensitivity(_get(message, "sensitivity")),
        internet_message_id=_get(message, "message_id") or _get(message, "internet_message_id"),
        in_reply_to=_get(message, "in_reply_to"),
        references=_references(_get(message, "references")),
        raw_headers=_headers(_get(message, "headers") or _get(message, "raw_headers")),
        attachments=attachments,
        links=_links(text, html),
        ews_id=remote_id,
        change_key=_changekey(message),
        extended_properties=_extended_properties(message),
    )


def _timezone_name(value: object | None, fallback: str) -> str:
    tzinfo = getattr(value, "tzinfo", None)
    return str(getattr(tzinfo, "key", None) or tzinfo or fallback)


def _datetime(value: date | datetime, timezone: str) -> datetime:
    if isinstance(value, datetime):
        normalized = domain_datetime(value)
        assert normalized is not None
        return normalized
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return datetime.combine(value, time.min, tzinfo=zone)


_WEEKDAY_BY_NUMBER = {
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
    7: "sunday",
    8: "day",
    9: "weekday",
    10: "weekend_day",
}
_WEEK_INDEX_BY_NUMBER = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "last"}
_MONTH_BY_NUMBER = {
    index: name
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


def _enum_token(value: object, aliases: Mapping[int, str]) -> str:
    if isinstance(value, int):
        return aliases[value]
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
    compact = normalized.replace("_", "")
    for candidate in aliases.values():
        if normalized == candidate or compact == candidate.replace("_", ""):
            return candidate
    raise ValueError(f"Unknown recurrence enum value: {value!r}")


def _recurrence_date(value: object, timezone: str = "UTC") -> date:
    if isinstance(value, datetime):
        normalized = domain_datetime(value)
        assert normalized is not None
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(ZoneInfo(timezone))
        return normalized.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _recurrence_int(value: object | None, *, label: str) -> int:
    if value is None:
        raise ValueError(f"Recurrence {label} is required.")
    return int(str(value))


def _recurrence(
    value: object | None,
    *,
    task: bool,
    timezone: str = "UTC",
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    fallback = {
        "type": type(value).__name__,
        "description": sanitize_terminal(value, max_length=4_096),
    }
    pattern = _get(value, "pattern")
    boundary = _get(value, "boundary")
    if pattern is None or boundary is None:
        return fallback
    try:
        pattern_name = type(pattern).__name__
        if task and pattern_name in {
            "DailyRegeneration",
            "WeeklyRegeneration",
            "MonthlyRegeneration",
            "YearlyRegeneration",
        }:
            pattern_mapping: dict[str, object] = {
                "type": "task_regeneration",
                "unit": {
                    "DailyRegeneration": "day",
                    "WeeklyRegeneration": "week",
                    "MonthlyRegeneration": "month",
                    "YearlyRegeneration": "year",
                }[pattern_name],
                "interval": _recurrence_int(
                    _get(pattern, "interval"), label="regeneration interval"
                ),
            }
        elif pattern_name == "DailyPattern":
            pattern_mapping = {
                "type": "daily",
                "interval": _recurrence_int(_get(pattern, "interval"), label="interval"),
            }
        elif pattern_name == "WeeklyPattern":
            weekdays = tuple(
                _enum_token(item, _WEEKDAY_BY_NUMBER) for item in (_get(pattern, "weekdays") or ())
            )
            pattern_mapping = {
                "type": "weekly",
                "interval": _recurrence_int(_get(pattern, "interval"), label="interval"),
                "weekdays": weekdays,
                "first_day_of_week": _enum_token(
                    _get(pattern, "first_day_of_week") or 1,
                    _WEEKDAY_BY_NUMBER,
                ),
            }
        elif pattern_name == "AbsoluteMonthlyPattern":
            pattern_mapping = {
                "type": "absolute_monthly",
                "interval": _recurrence_int(_get(pattern, "interval"), label="interval"),
                "day_of_month": _recurrence_int(
                    _get(pattern, "day_of_month"), label="day of month"
                ),
            }
        elif pattern_name == "RelativeMonthlyPattern":
            pattern_mapping = {
                "type": "relative_monthly",
                "interval": _recurrence_int(_get(pattern, "interval"), label="interval"),
                "weekday": _enum_token(_get(pattern, "weekday"), _WEEKDAY_BY_NUMBER),
                "week_index": _enum_token(
                    _get(pattern, "week_number"),
                    _WEEK_INDEX_BY_NUMBER,
                ),
            }
        elif pattern_name == "AbsoluteYearlyPattern":
            pattern_mapping = {
                "type": "absolute_yearly",
                "month": _enum_token(_get(pattern, "month"), _MONTH_BY_NUMBER),
                "day_of_month": _recurrence_int(
                    _get(pattern, "day_of_month"), label="day of month"
                ),
            }
        elif pattern_name == "RelativeYearlyPattern":
            pattern_mapping = {
                "type": "relative_yearly",
                "month": _enum_token(_get(pattern, "month"), _MONTH_BY_NUMBER),
                "weekday": _enum_token(_get(pattern, "weekday"), _WEEKDAY_BY_NUMBER),
                "week_index": _enum_token(
                    _get(pattern, "week_number"),
                    _WEEK_INDEX_BY_NUMBER,
                ),
            }
        else:
            return fallback

        start = _recurrence_date(_get(boundary, "start"), timezone)
        boundary_name = type(boundary).__name__
        if boundary_name == "NumberedPattern":
            boundary_mapping: dict[str, object] = {
                "type": "numbered",
                "start": start.isoformat(),
                "occurrences": _recurrence_int(_get(boundary, "number"), label="occurrence count"),
            }
        elif boundary_name == "EndDatePattern":
            boundary_mapping = {
                "type": "end_date",
                "start": start.isoformat(),
                "end": _recurrence_date(_get(boundary, "end"), timezone).isoformat(),
            }
        elif boundary_name == "NoEndPattern":
            boundary_mapping = {"type": "no_end", "start": start.isoformat()}
        else:
            return fallback
        canonical = {
            "schema_version": 1,
            "target": "task" if task else "calendar",
            "pattern": pattern_mapping,
            "boundary": boundary_mapping,
        }
        return normalize_recurrence_mapping(canonical, task=task)
    except (KeyError, TypeError, ValueError):
        # A newer server/library recurrence kind must not make an otherwise
        # readable item disappear.  Preserve a safe description and report it as
        # unsupported to higher layers instead.
        return fallback


def map_calendar_history(
    item: object,
    *,
    requested_reference: str,
    series_reference: str,
    observed_at: datetime,
    limit: int = 100,
    reference_factory: Callable[[object], str | None] | None = None,
) -> CalendarSeriesHistory:
    """Keep unknown/empty/truncated exception metadata distinct, without bodies."""
    limit = history_limit(limit)

    def timestamp(value: object | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Calendar occurrence metadata contains an invalid timestamp.")
        return domain_datetime(value)

    def occurrence(value: object) -> CalendarOccurrenceInfo:
        start = timestamp(_get(value, "start"))
        end = timestamp(_get(value, "end"))
        original_start = timestamp(_get(value, "original_start"))
        remote_id = _id(value)
        if remote_id is None and start is None and end is None and original_start is None:
            raise ValueError("Calendar occurrence metadata has no identity or timestamp.")
        reference = (
            reference_factory(value)
            if remote_id and reference_factory is not None
            else make_local_reference("event", remote_id)
            if remote_id
            else None
        )
        return CalendarOccurrenceInfo(reference, start, end, original_start)

    def collection(name: str) -> tuple[tuple[CalendarOccurrenceInfo, ...], bool, bool]:
        values = _get(item, name)
        if values is None:
            return (), False, False
        if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Iterable):
            raise ValueError("Calendar occurrence metadata contains an invalid list.")
        bounded = tuple(islice(values, limit + 1))
        return tuple(occurrence(value) for value in bounded[:limit]), True, len(bounded) > limit

    modified, modified_available, modified_truncated = collection("modified_occurrences")
    deleted, deleted_available, deleted_truncated = collection("deleted_occurrences")
    first, last = _get(item, "first_occurrence"), _get(item, "last_occurrence")
    return CalendarSeriesHistory(
        requested_reference=requested_reference,
        series_reference=series_reference,
        item_type=sanitize_terminal(_get(item, "type") or "unknown", max_length=80),
        observed_at=observed_at,
        limit_per_list=limit,
        first_occurrence=occurrence(first) if first is not None else None,
        last_occurrence=occurrence(last) if last is not None else None,
        modified_occurrences=modified,
        deleted_occurrences=deleted,
        modified_available=modified_available,
        deleted_available=deleted_available,
        modified_truncated=modified_truncated,
        deleted_truncated=deleted_truncated,
    )


def map_calendar_event(
    item: object,
    *,
    reference: str | None = None,
    timezone: str = "UTC",
) -> CalendarEvent:
    remote_id = _id(item)
    reference = reference or make_local_reference("calendar", remote_id or "unknown")
    start_raw = _get(item, "start")
    end_raw = _get(item, "end")
    if start_raw is None or end_raw is None:
        raise ValueError("Calendar items must have start and end values")
    text, _html = body_parts(item)
    event_timezone = str(
        getattr(_get(item, "_start_timezone"), "key", None) or _timezone_name(start_raw, timezone)
    )
    return CalendarEvent(
        reference=reference,
        subject=sanitize_terminal(_get(item, "subject", "") or "", max_length=2_048),
        start=_datetime(start_raw, event_timezone),
        end=_datetime(end_raw, event_timezone),
        timezone=event_timezone,
        location=sanitize_terminal(_get(item, "location"), max_length=2_048)
        if _get(item, "location")
        else None,
        body=text,
        organizer=map_address(_get(item, "organizer")),
        required_attendees=map_addresses(_get(item, "required_attendees")),
        optional_attendees=map_addresses(_get(item, "optional_attendees")),
        resources=map_addresses(_get(item, "resources")),
        is_all_day=bool(_get(item, "is_all_day", False)),
        is_meeting=bool(_get(item, "is_meeting", False)),
        recurrence=_recurrence(_get(item, "recurrence"), task=False, timezone=event_timezone),
        categories=_categories(_get(item, "categories")),
        importance=_importance(_get(item, "importance")),
        sensitivity=_sensitivity(_get(item, "sensitivity")),
        reminder_minutes=(
            _get(item, "reminder_minutes_before_start")
            if bool(_get(item, "reminder_is_set", False))
            else None
        ),
        availability=normalize_availability(_get(item, "legacy_free_busy_status") or "busy"),
        attachments=map_attachments(_get(item, "attachments"), parent_remote_id=remote_id),
        response_status=(
            str(_get(item, "my_response_type")) if _get(item, "my_response_type") else None
        ),
        item_type=str(_get(item, "type")) if _get(item, "type") else None,
        recurrence_id=domain_datetime(_get(item, "recurrence_id")),
        uid=str(_get(item, "uid")) if _get(item, "uid") else None,
    )


def _indexed_mapping(values: Iterable[object] | None, value_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, item in enumerate(_bounded(values, 100), start=1):
        label = str(_get(item, "label", index))
        raw = _get(item, value_name)
        if raw:
            result[label] = sanitize_terminal(raw, max_length=2_048)
    return result


def _physical_addresses(values: Iterable[object] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, item in enumerate(_bounded(values, 20), start=1):
        label = str(_get(item, "label", index))
        parts = [
            _get(item, "street"),
            _get(item, "city"),
            _get(item, "state"),
            _get(item, "zipcode"),
            _get(item, "country"),
        ]
        rendered = ", ".join(sanitize_terminal(part, max_length=1_024) for part in parts if part)
        if rendered:
            result[label] = rendered
    return result


def map_contact(item: object, *, reference: str | None = None) -> ContactInfo:
    remote_id = _id(item)
    reference = reference or make_local_reference("contact", remote_id or "unknown")
    display_name = sanitize_terminal(
        _get(item, "display_name")
        or " ".join(str(part) for part in (_get(item, "given_name"), _get(item, "surname")) if part)
        or "Unnamed contact",
        max_length=1_024,
    )
    emails = tuple(
        Address(email=value, name=display_name)
        for value in _indexed_mapping(_get(item, "email_addresses"), "email").values()
    )
    text, _html = body_parts(item)
    photo = _get(item, "photo")
    return ContactInfo(
        reference=reference,
        display_name=display_name,
        given_name=_get(item, "given_name"),
        middle_name=_get(item, "middle_name"),
        family_name=_get(item, "surname") or _get(item, "family_name"),
        nickname=_get(item, "nickname"),
        emails=emails,
        phones=_indexed_mapping(_get(item, "phone_numbers"), "phone_number"),
        addresses=_physical_addresses(_get(item, "physical_addresses")),
        company=_get(item, "company_name") or _get(item, "company"),
        department=_get(item, "department"),
        job_title=_get(item, "job_title"),
        birthday=domain_date(_get(item, "birthday")),
        notes=text or _get(item, "notes"),
        categories=_categories(_get(item, "categories")),
        has_photo=bool(_get(item, "has_picture") or photo),
        photo_size=_get(photo, "size") if photo is not None else None,
        photo_content_type=_get(photo, "content_type") if photo is not None else None,
    )


def map_task(item: object, *, reference: str | None = None) -> TaskInfo:
    remote_id = _id(item)
    reference = reference or make_local_reference("task", remote_id or "unknown")
    percent = _get(item, "percent_complete", 0) or 0
    if isinstance(percent, Decimal):
        percent = int(percent.to_integral_value())
    else:
        percent = round(float(percent))
    text, _html = body_parts(item)
    return TaskInfo(
        reference=reference,
        subject=sanitize_terminal(_get(item, "subject", "") or "", max_length=2_048),
        status=str(_get(item, "status", "NotStarted")),
        percent_complete=min(max(percent, 0), 100),
        start_date=domain_date(_get(item, "start_date")),
        due_date=domain_date(_get(item, "due_date")),
        completed_at=domain_datetime(_get(item, "complete_date") or _get(item, "completed_at")),
        body=text,
        categories=_categories(_get(item, "categories")),
        importance=_importance(_get(item, "importance")),
        reminder_at=(
            domain_datetime(_get(item, "reminder_due_by"))
            if bool(_get(item, "reminder_is_set", False))
            else None
        ),
        recurrence=_recurrence(_get(item, "recurrence"), task=True),
    )


# Readable aliases used by integrations and older fixture suites.
address_from_ews = map_address
attachment_from_ews = map_attachment
calendar_event_from_ews = map_calendar_event
contact_from_ews = map_contact
folder_from_ews = map_folder
message_detail_from_ews = map_message_detail
message_summary_from_ews = map_message_summary
task_from_ews = map_task


__all__ = [
    "ReferenceFactory",
    "address_from_ews",
    "attachment_from_ews",
    "body_parts",
    "calendar_event_from_ews",
    "contact_from_ews",
    "folder_from_ews",
    "is_item_attachment",
    "make_local_reference",
    "map_address",
    "map_addresses",
    "map_attachment",
    "map_attachments",
    "map_calendar_event",
    "map_contact",
    "map_folder",
    "map_message_detail",
    "map_message_summary",
    "map_task",
    "message_detail_from_ews",
    "message_summary_from_ews",
    "remote_identity",
    "task_from_ews",
]
