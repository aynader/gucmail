"""Safe, dependency-light calendar and contact interchange helpers.

The functions in this module deliberately operate on GUC Mail's domain models.  They do
not know about Exchange, folders, or network sessions, which makes them suitable for
previewing imports before a mutating gateway call.
"""

from __future__ import annotations

import hashlib
import quopri
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import tz

from gucmail.errors import ConfigurationError
from gucmail.models import (
    Address,
    CalendarEvent,
    ContactInfo,
    Importance,
    ItemSource,
    Sensitivity,
)
from gucmail.services.recurrence import RecurrenceTarget, recurrence_to_rfc_mapping

MAX_INTERCHANGE_BYTES = 5 * 1024 * 1024
MAX_INTERCHANGE_RECORDS = 10_000
_ICAL_DATE_RE = re.compile(r"^\d{8}$")
_ICAL_DATETIME_RE = re.compile(r"^\d{8}T\d{4}(?:\d{2})?(?:Z|[+-]\d{4})?$")


def _validate_size(value: str, *, kind: str, maximum_bytes: int) -> None:
    if "\x00" in value:
        raise ConfigurationError(f"The {kind} input contains a NUL byte.")
    if len(value.encode("utf-8")) > maximum_bytes:
        raise ConfigurationError(f"The {kind} input exceeds the {maximum_bytes}-byte safety limit.")


def _unfold_lines(value: str) -> list[str]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    unfolded: list[str] = []
    for line in normalized.split("\n"):
        if line.startswith((" ", "\t")):
            if not unfolded:
                raise ConfigurationError("A folded interchange line has no preceding line.")
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _split_quoted(value: str, separator: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == separator and not quoted:
            result.append("".join(current))
            current = []
        else:
            current.append(char)
    result.append("".join(current))
    return result


def _parse_content_line(line: str) -> tuple[str, dict[str, tuple[str, ...]], str]:
    quoted = False
    escaped = False
    delimiter = -1
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            delimiter = index
            break
    if delimiter < 1:
        raise ConfigurationError(f"Malformed interchange content line: {line[:80]!r}")
    left, raw_value = line[:delimiter], line[delimiter + 1 :]
    parts = _split_quoted(left, ";")
    name = parts[0].upper()
    params: dict[str, tuple[str, ...]] = {}
    for raw_parameter in parts[1:]:
        if "=" not in raw_parameter:
            params.setdefault("TYPE", ())
            params["TYPE"] += (raw_parameter.strip('"').upper(),)
            continue
        key, parameter_value = raw_parameter.split("=", 1)
        values = tuple(
            item.strip().strip('"') for item in _split_quoted(parameter_value, ",") if item.strip()
        )
        params[key.upper()] = values
    return name, params, raw_value


def _unescape_text(value: str) -> str:
    # Order matters: protect escaped backslashes while interpreting the remaining values.
    marker = "\x1f"
    value = value.replace("\\\\", marker)
    value = re.sub(r"\\[nN]", "\n", value)
    value = value.replace("\\,", ",").replace("\\;", ";").replace("\\:", ":")
    return value.replace(marker, "\\")


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _escape_parameter(value: str) -> str:
    cleaned = value.replace("\r", " ").replace("\n", " ").replace('"', "'")
    if any(char in cleaned for char in ":;, "):
        return f'"{cleaned}"'
    return cleaned


def _fold_line(line: str, *, width: int = 75) -> list[str]:
    if width < 4:
        raise ValueError("Fold width must be at least four octets.")
    result: list[str] = []
    current = ""
    current_bytes = 0
    limit = width
    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > limit:
            result.append(current)
            current = " " + char
            current_bytes = 1 + char_bytes
            limit = width
        else:
            current += char
            current_bytes += char_bytes
    result.append(current)
    return result


def _render_lines(lines: Iterable[str]) -> str:
    folded: list[str] = []
    for line in lines:
        if "\r" in line or "\n" in line:
            raise ValueError("Rendered content lines may not contain literal newlines.")
        folded.extend(_fold_line(line))
    return "\r\n".join(folded) + "\r\n"


def _zone(zone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown time zone: {zone_name}") from exc


def _attach_zone_strict(value: datetime, zone_name: str) -> datetime:
    zone = _zone(zone_name)
    dateutil_zone = tz.gettz(zone_name)
    if dateutil_zone is not None:
        tentative = value.replace(tzinfo=dateutil_zone)
        if not tz.datetime_exists(tentative):
            raise ConfigurationError(
                f"{value.isoformat(sep=' ')} does not exist in {zone_name} because of a "
                "daylight-saving transition."
            )
        if tz.datetime_ambiguous(tentative):
            raise ConfigurationError(
                f"{value.isoformat(sep=' ')} occurs twice in {zone_name}; include an explicit "
                "UTC offset."
            )
    return value.replace(tzinfo=zone)


def _parse_ical_datetime(
    value: str,
    params: Mapping[str, Sequence[str]],
    *,
    default_timezone: str,
) -> tuple[datetime, bool, str]:
    raw = value.strip()
    declared_date = any(item.upper() == "DATE" for item in params.get("VALUE", ()))
    if declared_date or _ICAL_DATE_RE.fullmatch(raw):
        if not _ICAL_DATE_RE.fullmatch(raw):
            raise ConfigurationError(f"Invalid iCalendar DATE value: {raw!r}")
        parsed_date = datetime.strptime(raw, "%Y%m%d").date()
        zone_name = params.get("TZID", (default_timezone,))[0]
        return (
            _attach_zone_strict(datetime.combine(parsed_date, time.min), zone_name),
            True,
            zone_name,
        )
    if not _ICAL_DATETIME_RE.fullmatch(raw):
        raise ConfigurationError(f"Invalid iCalendar date-time value: {raw!r}")
    value_without_zone = raw
    if raw.endswith("Z"):
        value_without_zone = raw[:-1]
        parsed = datetime.strptime(
            value_without_zone, "%Y%m%dT%H%M%S" if len(value_without_zone) == 15 else "%Y%m%dT%H%M"
        ).replace(tzinfo=UTC)
        return parsed, False, "UTC"
    offset_match = re.search(r"([+-]\d{4})$", raw)
    if offset_match:
        value_without_zone = raw[: -len(offset_match.group(1))]
        fmt = "%Y%m%dT%H%M%S%z" if len(value_without_zone) == 15 else "%Y%m%dT%H%M%z"
        parsed = datetime.strptime(raw, fmt).astimezone(UTC)
        return parsed, False, "UTC"
    fmt = "%Y%m%dT%H%M%S" if len(raw) == 15 else "%Y%m%dT%H%M"
    naive = datetime.strptime(raw, fmt)
    zone_name = params.get("TZID", (default_timezone,))[0]
    return _attach_zone_strict(naive, zone_name), False, zone_name


def _ical_datetime_line(name: str, value: datetime, timezone: str, *, all_day: bool) -> str:
    if value.tzinfo is None:
        raise ConfigurationError(f"{name} must contain a time-zone-aware date and time.")
    if all_day:
        return f"{name};VALUE=DATE:{value.astimezone(_zone(timezone)):%Y%m%d}"
    if timezone.upper() == "UTC":
        return f"{name}:{value.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    prefix = f"{name};TZID={_escape_parameter(timezone)}:"
    return prefix + f"{value.astimezone(_zone(timezone)):%Y%m%dT%H%M%S}"


def _parse_rrule(value: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for component in value.split(";"):
        if not component or "=" not in component:
            raise ConfigurationError(f"Malformed iCalendar recurrence rule: {value!r}")
        key, raw = component.split("=", 1)
        key = key.strip().lower()
        values = tuple(item for item in raw.split(",") if item)
        if key in {"count", "interval"} and len(values) == 1:
            try:
                result[key] = int(values[0])
            except ValueError as exc:
                raise ConfigurationError(f"Invalid recurrence integer: {values[0]!r}") from exc
        elif len(values) == 1:
            result[key] = values[0].lower() if key == "freq" else values[0]
        else:
            result[key] = values
    return result


def _format_rrule(recurrence: Mapping[str, Any]) -> str:
    components: list[str] = []
    keys = sorted(recurrence, key=lambda item: (item.lower() != "freq", item.lower()))
    for key in keys:
        value = recurrence[key]
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            rendered = ",".join(str(item).upper() for item in value)
        else:
            rendered = str(value).upper()
        components.append(f"{str(key).upper()}={rendered}")
    if not components:
        raise ConfigurationError("A recurrence rule may not be empty.")
    return ";".join(components)


def parse_ics(
    value: str,
    *,
    default_timezone: str = "UTC",
    maximum_bytes: int = MAX_INTERCHANGE_BYTES,
    maximum_events: int = MAX_INTERCHANGE_RECORDS,
) -> tuple[CalendarEvent, ...]:
    """Parse VEVENT records from an RFC 5545 document into domain events.

    Unknown properties are ignored.  Ambiguous and nonexistent wall times are rejected
    instead of silently choosing one side of a daylight-saving transition.
    """

    _validate_size(value, kind="iCalendar", maximum_bytes=maximum_bytes)
    _zone(default_timezone)
    lines = _unfold_lines(value)
    events: list[CalendarEvent] = []
    current: list[tuple[str, dict[str, tuple[str, ...]], str]] | None = None
    alarm_depth = 0
    for raw_line in lines:
        if not raw_line:
            continue
        name, params, raw = _parse_content_line(raw_line)
        upper_value = raw.upper()
        if name == "BEGIN" and upper_value == "VEVENT":
            if current is not None:
                raise ConfigurationError("Nested VEVENT records are not valid.")
            current = []
            alarm_depth = 0
            continue
        if name == "END" and upper_value == "VEVENT":
            if current is None:
                raise ConfigurationError("VEVENT ended without beginning.")
            events.append(_event_from_properties(current, default_timezone=default_timezone))
            if len(events) > maximum_events:
                raise ConfigurationError(
                    f"The iCalendar input exceeds the {maximum_events}-event safety limit."
                )
            current = None
            continue
        if current is None:
            continue
        if name == "BEGIN" and upper_value == "VALARM":
            alarm_depth += 1
            continue
        if name == "END" and upper_value == "VALARM":
            alarm_depth = max(alarm_depth - 1, 0)
            continue
        if alarm_depth:
            current.append((f"VALARM-{name}", params, raw))
        else:
            current.append((name, params, raw))
    if current is not None:
        raise ConfigurationError("The iCalendar input contains an unterminated VEVENT.")
    if not events:
        raise ConfigurationError("The iCalendar input does not contain a VEVENT.")
    return tuple(events)


def _event_from_properties(
    properties: Sequence[tuple[str, Mapping[str, Sequence[str]], str]],
    *,
    default_timezone: str,
) -> CalendarEvent:
    grouped: dict[str, list[tuple[Mapping[str, Sequence[str]], str]]] = {}
    for name, params, value in properties:
        grouped.setdefault(name, []).append((params, value))
    if "DTSTART" not in grouped:
        raise ConfigurationError("An imported event is missing DTSTART.")
    start, all_day, timezone = _parse_ical_datetime(
        grouped["DTSTART"][0][1], grouped["DTSTART"][0][0], default_timezone=default_timezone
    )
    if "DTEND" in grouped:
        end, end_is_date, _ = _parse_ical_datetime(
            grouped["DTEND"][0][1], grouped["DTEND"][0][0], default_timezone=timezone
        )
        if end_is_date != all_day:
            raise ConfigurationError("DTSTART and DTEND must both be dates or both be date-times.")
    elif "DURATION" in grouped:
        duration = _parse_ical_duration(grouped["DURATION"][0][1])
        end = start + duration
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ConfigurationError("An imported event must end after it starts.")

    def text_property(name: str, default: str = "") -> str:
        values = grouped.get(name)
        return _unescape_text(values[0][1]) if values else default

    organizer = _address_from_ical(grouped["ORGANIZER"][0]) if grouped.get("ORGANIZER") else None
    required: list[Address] = []
    optional: list[Address] = []
    resources: list[Address] = []
    for attendee in grouped.get("ATTENDEE", []):
        role = next(iter(attendee[0].get("ROLE", ("REQ-PARTICIPANT",))), "REQ-PARTICIPANT").upper()
        address = _address_from_ical(attendee)
        if role == "OPT-PARTICIPANT":
            optional.append(address)
        elif role in {"NON-PARTICIPANT", "CHAIR"} or any(
            item.upper() == "RESOURCE" for item in attendee[0].get("CUTYPE", ())
        ):
            resources.append(address)
        else:
            required.append(address)
    category_values: list[str] = []
    for _, raw_categories in grouped.get("CATEGORIES", []):
        category_values.extend(_unescape_text(item) for item in _split_escaped(raw_categories, ","))
    recurrence = _parse_rrule(grouped["RRULE"][0][1]) if grouped.get("RRULE") else None
    reminder = None
    if grouped.get("VALARM-TRIGGER"):
        trigger = grouped["VALARM-TRIGGER"][0][1]
        delta = _parse_ical_duration(trigger)
        reminder = max(0, round(abs(delta.total_seconds()) / 60))
    importance = _priority_to_importance(text_property("PRIORITY", "5"))
    sensitivity_raw = text_property("CLASS", "PUBLIC").lower()
    sensitivity = {
        "private": Sensitivity.PRIVATE,
        "confidential": Sensitivity.CONFIDENTIAL,
    }.get(sensitivity_raw, Sensitivity.NORMAL)
    availability = text_property("X-MICROSOFT-CDO-BUSYSTATUS", "").lower()
    if not availability:
        availability = (
            "free" if text_property("TRANSP", "OPAQUE").upper() == "TRANSPARENT" else "busy"
        )
    uid = text_property("UID")
    if not uid:
        seed = f"{text_property('SUMMARY')}\0{start.isoformat()}\0{end.isoformat()}"
        uid = "ics:" + hashlib.sha256(seed.encode()).hexdigest()[:24]
    return CalendarEvent(
        reference=uid,
        subject=text_property("SUMMARY", "(untitled event)"),
        start=start,
        end=end,
        timezone=timezone,
        location=text_property("LOCATION") or None,
        body=text_property("DESCRIPTION"),
        organizer=organizer,
        required_attendees=tuple(required),
        optional_attendees=tuple(optional),
        resources=tuple(resources),
        is_all_day=all_day,
        is_meeting=bool(required or optional or resources or organizer),
        recurrence=recurrence,
        categories=tuple(dict.fromkeys(item for item in category_values if item)),
        importance=importance,
        sensitivity=sensitivity,
        reminder_minutes=reminder,
        availability=availability,
        source=ItemSource.LOCAL,
    )


def _split_escaped(value: str, separator: str) -> list[str]:
    result: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.extend(("\\", char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == separator:
            result.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    result.append("".join(current))
    return result


def _parse_ical_duration(value: str) -> timedelta:
    match = re.fullmatch(
        r"(?P<sign>[+-])?P(?:(?P<weeks>\d+)W|(?:(?P<days>\d+)D)?"
        r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?)",
        value.upper(),
    )
    if not match or not any(
        match.group(name) for name in ("weeks", "days", "hours", "minutes", "seconds")
    ):
        raise ConfigurationError(f"Invalid iCalendar duration: {value!r}")
    delta = timedelta(
        weeks=int(match.group("weeks") or 0),
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=int(match.group("seconds") or 0),
    )
    return -delta if match.group("sign") == "-" else delta


def _address_from_ical(value: tuple[Mapping[str, Sequence[str]], str]) -> Address:
    params, raw = value
    email = raw.strip()
    if email.lower().startswith("mailto:"):
        email = email[7:]
    if not email or "\r" in email or "\n" in email:
        raise ConfigurationError("An iCalendar attendee has an invalid email address.")
    name = next(iter(params.get("CN", ())), None)
    return Address(email=email, name=_unescape_text(name) if name else None)


def _priority_to_importance(value: str) -> Importance:
    try:
        priority = int(value)
    except ValueError:
        return Importance.NORMAL
    if 1 <= priority <= 4:
        return Importance.HIGH
    if 6 <= priority <= 9:
        return Importance.LOW
    return Importance.NORMAL


def export_ics(
    events: CalendarEvent | Iterable[CalendarEvent],
    *,
    product_id: str = "-//GUC Mail CLI//Calendar Export//EN",
) -> str:
    """Export one or more domain events as a deterministic RFC 5545 calendar."""

    event_values = (events,) if isinstance(events, CalendarEvent) else tuple(events)
    if not event_values:
        raise ConfigurationError("At least one calendar event is required for export.")
    if len(event_values) > MAX_INTERCHANGE_RECORDS:
        raise ConfigurationError("Too many events were requested for one export.")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_escape_text(product_id)}",
        "CALSCALE:GREGORIAN",
    ]
    for event in event_values:
        if (
            event.start.tzinfo is not None
            and event.end.tzinfo is not None
            and event.end.astimezone(UTC) <= event.start.astimezone(UTC)
        ):
            raise ConfigurationError(f"Event {event.reference!r} must end after it starts.")
        _zone(event.timezone)
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{_escape_text(event.reference)}",
                _ical_datetime_line(
                    "DTSTART", event.start, event.timezone, all_day=event.is_all_day
                ),
                _ical_datetime_line("DTEND", event.end, event.timezone, all_day=event.is_all_day),
                f"SUMMARY:{_escape_text(event.subject)}",
            ]
        )
        if event.location:
            lines.append(f"LOCATION:{_escape_text(event.location)}")
        if event.body:
            lines.append(f"DESCRIPTION:{_escape_text(event.body)}")
        if event.organizer:
            lines.append(_render_ical_address("ORGANIZER", event.organizer))
        for attendee in event.required_attendees:
            lines.append(_render_ical_address("ATTENDEE", attendee, role="REQ-PARTICIPANT"))
        for attendee in event.optional_attendees:
            lines.append(_render_ical_address("ATTENDEE", attendee, role="OPT-PARTICIPANT"))
        for attendee in event.resources:
            lines.append(
                _render_ical_address("ATTENDEE", attendee, role="NON-PARTICIPANT", resource=True)
            )
        if event.recurrence:
            # CalendarEvent accepts both the RFC-shaped mapping produced by this
            # module's parser and the canonical, versioned recurrence mapping used
            # by the service/EWS layers.  Rendering the canonical mapping directly
            # would produce invalid components such as ``PATTERN={...}``.
            recurrence = recurrence_to_rfc_mapping(
                event.recurrence,
                target=RecurrenceTarget.CALENDAR,
                start=event.start,
                all_day=event.is_all_day,
            )
            lines.append(f"RRULE:{_format_rrule(recurrence)}")
        if event.categories:
            lines.append("CATEGORIES:" + ",".join(_escape_text(item) for item in event.categories))
        lines.append(
            "PRIORITY:"
            + {Importance.HIGH: "1", Importance.NORMAL: "5", Importance.LOW: "9"}[event.importance]
        )
        if event.sensitivity in {Sensitivity.PRIVATE, Sensitivity.CONFIDENTIAL}:
            lines.append(f"CLASS:{event.sensitivity.value.upper()}")
        lines.append(
            "TRANSP:TRANSPARENT" if event.availability.lower() == "free" else "TRANSP:OPAQUE"
        )
        lines.append(f"X-MICROSOFT-CDO-BUSYSTATUS:{event.availability.upper()}")
        if event.reminder_minutes is not None:
            if event.reminder_minutes < 0:
                raise ConfigurationError("Reminder minutes may not be negative.")
            lines.extend(
                [
                    "BEGIN:VALARM",
                    f"TRIGGER:-PT{event.reminder_minutes}M",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_escape_text(event.subject)}",
                    "END:VALARM",
                ]
            )
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return _render_lines(lines)


def _render_ical_address(
    property_name: str,
    address: Address,
    *,
    role: str | None = None,
    resource: bool = False,
) -> str:
    if not address.email or any(char in address.email for char in "\r\n"):
        raise ConfigurationError("Calendar addresses must have a valid email value.")
    params: list[str] = []
    if address.name:
        params.append(f"CN={_escape_parameter(address.name)}")
    if role:
        params.append(f"ROLE={role}")
    if resource:
        params.append("CUTYPE=RESOURCE")
    rendered_params = ";" + ";".join(params) if params else ""
    return f"{property_name}{rendered_params}:mailto:{address.email}"


def parse_vcards(
    value: str,
    *,
    maximum_bytes: int = MAX_INTERCHANGE_BYTES,
    maximum_contacts: int = MAX_INTERCHANGE_RECORDS,
) -> tuple[ContactInfo, ...]:
    """Parse vCard 3.0/4.0 records into local contact domain objects."""

    _validate_size(value, kind="vCard", maximum_bytes=maximum_bytes)
    contacts: list[ContactInfo] = []
    current: list[tuple[str, dict[str, tuple[str, ...]], str]] | None = None
    for raw_line in _unfold_lines(value):
        if not raw_line:
            continue
        name, params, raw = _parse_content_line(raw_line)
        upper_value = raw.upper()
        if name == "BEGIN" and upper_value == "VCARD":
            if current is not None:
                raise ConfigurationError("Nested vCard records are not valid.")
            current = []
            continue
        if name == "END" and upper_value == "VCARD":
            if current is None:
                raise ConfigurationError("A vCard ended without beginning.")
            contacts.append(_contact_from_properties(current))
            if len(contacts) > maximum_contacts:
                raise ConfigurationError(
                    f"The vCard input exceeds the {maximum_contacts}-contact safety limit."
                )
            current = None
            continue
        if current is not None:
            current.append((name, params, raw))
    if current is not None:
        raise ConfigurationError("The vCard input contains an unterminated record.")
    if not contacts:
        raise ConfigurationError("The input does not contain a vCard record.")
    return tuple(contacts)


def parse_vcard(value: str, **kwargs: Any) -> ContactInfo:
    """Parse exactly one vCard record."""

    contacts = parse_vcards(value, **kwargs)
    if len(contacts) != 1:
        raise ConfigurationError(f"Expected one vCard, but found {len(contacts)} records.")
    return contacts[0]


def _contact_from_properties(
    properties: Sequence[tuple[str, Mapping[str, Sequence[str]], str]],
) -> ContactInfo:
    grouped: dict[str, list[tuple[Mapping[str, Sequence[str]], str]]] = {}
    for name, params, value in properties:
        grouped.setdefault(name.split(".")[-1], []).append((params, value))
    versions = grouped.get("VERSION")
    if not versions:
        raise ConfigurationError("A vCard record is missing VERSION.")
    version = versions[0][1].strip()
    if version not in {"2.1", "3.0", "4.0"}:
        raise ConfigurationError(f"Unsupported vCard version: {version!r}")

    def decode(params: Mapping[str, Sequence[str]], raw: str) -> str:
        encodings = {item.upper() for item in params.get("ENCODING", ())}
        data = (
            quopri.decodestring(raw).decode("utf-8", errors="replace")
            if "QUOTED-PRINTABLE" in encodings
            else raw
        )
        return _unescape_text(data)

    def first(name: str) -> str | None:
        values = grouped.get(name)
        return decode(*values[0]) if values else None

    name_parts = ["", "", "", "", ""]
    if grouped.get("N"):
        params, raw_name = grouped["N"][0]
        name_parts = [decode(params, item) for item in _split_escaped(raw_name, ";")]
        name_parts += [""] * (5 - len(name_parts))
    family, given, middle = name_parts[0], name_parts[1], name_parts[2]
    display_name = first("FN") or " ".join(item for item in (given, middle, family) if item)
    emails: list[Address] = []
    for params, raw_email in grouped.get("EMAIL", []):
        email = decode(params, raw_email).strip()
        if email.casefold().startswith("mailto:"):
            email = email[7:]
        if not email:
            continue
        types = params.get("TYPE", ())
        label = next((item.lower() for item in types if item.upper() != "INTERNET"), None)
        emails.append(Address(email=email, mailbox_type=label))
    phones: dict[str, str] = {}
    for index, (params, raw_phone) in enumerate(grouped.get("TEL", []), start=1):
        phone = decode(params, raw_phone).strip()
        if phone.casefold().startswith("tel:"):
            phone = phone[4:]
        if phone:
            label = next(iter(params.get("TYPE", (f"phone{index}",))), f"phone{index}").lower()
            phones[_unique_key(phones, label)] = phone
    addresses: dict[str, str] = {}
    for index, (params, raw_address) in enumerate(grouped.get("ADR", []), start=1):
        parts = [decode(params, item) for item in _split_escaped(raw_address, ";")]
        rendered = ", ".join(item for item in parts if item)
        if rendered:
            label = next(iter(params.get("TYPE", (f"address{index}",))), f"address{index}").lower()
            addresses[_unique_key(addresses, label)] = rendered
    company = None
    department = None
    if grouped.get("ORG"):
        params, raw_org = grouped["ORG"][0]
        org_parts = [decode(params, item) for item in _split_escaped(raw_org, ";")]
        company = org_parts[0] or None
        department = org_parts[1] or None if len(org_parts) > 1 else None
    birthday = _parse_vcard_date(first("BDAY"))
    categories: list[str] = []
    for params, raw_categories in grouped.get("CATEGORIES", []):
        categories.extend(decode(params, item) for item in _split_escaped(raw_categories, ","))
    uid = first("UID")
    if not uid:
        seed = "\0".join((display_name, *(address.email.lower() for address in emails)))
        uid = "vcard:" + hashlib.sha256(seed.encode()).hexdigest()[:24]
    return ContactInfo(
        reference=uid,
        display_name=display_name or "(unnamed contact)",
        given_name=given or None,
        middle_name=middle or None,
        family_name=family or None,
        nickname=first("NICKNAME"),
        emails=tuple(emails),
        phones=phones,
        addresses=addresses,
        company=company,
        department=department,
        job_title=first("TITLE"),
        birthday=birthday,
        notes=first("NOTE"),
        categories=tuple(dict.fromkeys(item for item in categories if item)),
        source=ItemSource.LOCAL,
    )


def _parse_vcard_date(value: str | None) -> date | None:
    if not value or value.startswith("--"):
        return None
    try:
        return (
            date.fromisoformat(value) if "-" in value else datetime.strptime(value, "%Y%m%d").date()
        )
    except ValueError as exc:
        raise ConfigurationError(f"Invalid vCard date: {value!r}") from exc


def _unique_key(values: Mapping[str, str], candidate: str) -> str:
    if candidate not in values:
        return candidate
    index = 2
    while f"{candidate}{index}" in values:
        index += 1
    return f"{candidate}{index}"


def export_vcards(
    contacts: ContactInfo | Iterable[ContactInfo],
    *,
    version: str = "4.0",
) -> str:
    """Export contacts as deterministic vCard 3.0 or 4.0 records."""

    if version not in {"3.0", "4.0"}:
        raise ConfigurationError("vCard export version must be '3.0' or '4.0'.")
    contact_values = (contacts,) if isinstance(contacts, ContactInfo) else tuple(contacts)
    if not contact_values:
        raise ConfigurationError("At least one contact is required for export.")
    if len(contact_values) > MAX_INTERCHANGE_RECORDS:
        raise ConfigurationError("Too many contacts were requested for one export.")
    lines: list[str] = []
    for contact in contact_values:
        lines.extend(
            [
                "BEGIN:VCARD",
                f"VERSION:{version}",
                f"UID:{_escape_text(contact.reference)}",
                f"FN:{_escape_text(contact.display_name)}",
                "N:"
                + ";".join(
                    _escape_text(value or "")
                    for value in (
                        contact.family_name,
                        contact.given_name,
                        contact.middle_name,
                        None,
                        None,
                    )
                ),
            ]
        )
        if contact.nickname:
            lines.append(f"NICKNAME:{_escape_text(contact.nickname)}")
        for address in contact.emails:
            label = _safe_vcard_type(address.mailbox_type)
            prefix = f"EMAIL;TYPE={label}" if label else "EMAIL"
            lines.append(f"{prefix}:{_escape_text(address.email)}")
        for label, phone in contact.phones.items():
            safe_label = _safe_vcard_type(label)
            prefix = f"TEL;TYPE={safe_label}" if safe_label else "TEL"
            value = f"tel:{phone}" if version == "4.0" else phone
            lines.append(f"{prefix}:{_escape_text(value)}")
        for label, postal_address in contact.addresses.items():
            safe_label = _safe_vcard_type(label)
            prefix = f"ADR;TYPE={safe_label}" if safe_label else "ADR"
            lines.append(f"{prefix}:;;{_escape_text(postal_address)};;;;")
        if contact.company or contact.department:
            lines.append(
                "ORG:"
                + ";".join(
                    _escape_text(value or "") for value in (contact.company, contact.department)
                )
            )
        if contact.job_title:
            lines.append(f"TITLE:{_escape_text(contact.job_title)}")
        if contact.birthday:
            lines.append(f"BDAY:{contact.birthday.isoformat()}")
        if contact.notes:
            lines.append(f"NOTE:{_escape_text(contact.notes)}")
        if contact.categories:
            lines.append(
                "CATEGORIES:" + ",".join(_escape_text(item) for item in contact.categories)
            )
        lines.append("END:VCARD")
    return _render_lines(lines)


def export_vcard(contact: ContactInfo, *, version: str = "4.0") -> str:
    """Export one contact as a vCard."""

    return export_vcards(contact, version=version)


def _safe_vcard_type(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9-]", "-", value).strip("-")
    return normalized.upper() or None


# Friendly aliases used by command and service layers.
import_ics = parse_ics
events_to_ics = export_ics
import_vcards = parse_vcards
contacts_to_vcards = export_vcards


__all__ = [
    "MAX_INTERCHANGE_BYTES",
    "MAX_INTERCHANGE_RECORDS",
    "contacts_to_vcards",
    "events_to_ics",
    "export_ics",
    "export_vcard",
    "export_vcards",
    "import_ics",
    "import_vcards",
    "parse_ics",
    "parse_vcard",
    "parse_vcards",
]
