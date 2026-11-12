"""Exchange-aware mailbox quota reporting kept distinct from local disk capacity."""

from __future__ import annotations

import html
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.models import (
    MailboxOperationStatus,
    MailboxQuotaStatus,
    QuotaConfidence,
    QuotaInfo,
    QuotaSource,
    QuotaThresholdKind,
    QuotaValue,
)

MEBIBYTE = 1024 * 1024
KNOWN_GUC_PROHIBIT_SEND_BYTES = 92 * MEBIBYTE
DEFAULT_FALLBACK_CAPACITY_BYTES = None  # Compatibility: Section 22 forbids invented capacity.


class QuotaStatus(StrEnum):
    EXACT = "exact"
    PARTIAL = "partial"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class QuotaEvent(StrEnum):
    WARNING = "warning_threshold_reached"
    PROHIBIT_SEND = "prohibit_send_reached"
    PROHIBIT_SEND_RECEIVE = "prohibit_send_receive_reached"


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """Sanitized historical evidence parsed from one genuine Exchange quota notice."""

    event: QuotaEvent
    threshold_kind: QuotaThresholdKind
    historical_usage_bytes: int
    threshold_bytes: int
    source: QuotaSource = QuotaSource.SERVER_NOTIFICATION
    confidence: QuotaConfidence = QuotaConfidence.HIGH
    observed_at: datetime | None = None
    evidence: str = "Exchange-generated quota notification"

    def __post_init__(self) -> None:
        if self.historical_usage_bytes < 0 or self.threshold_bytes <= 0:
            raise ValueError("Quota-notification values must be positive.")
        expected_kind = {
            QuotaEvent.WARNING: QuotaThresholdKind.ISSUE_WARNING,
            QuotaEvent.PROHIBIT_SEND: QuotaThresholdKind.PROHIBIT_SEND,
            QuotaEvent.PROHIBIT_SEND_RECEIVE: QuotaThresholdKind.PROHIBIT_SEND_RECEIVE,
        }[self.event]
        if self.threshold_kind is not expected_kind:
            raise ValueError("Quota event and threshold kind do not match.")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("Quota observation timestamps must include a time zone.")


@dataclass(frozen=True, slots=True)
class QuotaOverrides:
    """Explicit per-threshold settings; absent fields remain genuinely unknown."""

    issue_warning_bytes: int | None = None
    prohibit_send_bytes: int | None = None
    prohibit_send_receive_bytes: int | None = None

    def __post_init__(self) -> None:
        values = (
            self.issue_warning_bytes,
            self.prohibit_send_bytes,
            self.prohibit_send_receive_bytes,
        )
        if any(value is not None and value <= 0 for value in values):
            raise ValueError("User-configured quota thresholds must be positive.")


@dataclass(frozen=True, slots=True)
class LocalDiskUsage:
    label: str
    path: Path
    available: bool
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class QuotaReport:
    status: QuotaStatus
    mailbox: QuotaInfo | None
    unavailable_reason: str | None = None
    local_disks: tuple[LocalDiskUsage, ...] = ()
    observations: tuple[QuotaObservation, ...] = ()
    fallback_capacity: QuotaValue = field(default_factory=lambda: QuotaValue(None))
    mailbox_status: MailboxQuotaStatus = field(init=False)
    send_status: MailboxOperationStatus = field(init=False)
    receive_status: MailboxOperationStatus = field(init=False)
    send_threshold_percent: float | None = field(init=False)
    over_send_limit_bytes: int | None = field(init=False)
    remaining_to_send_limit_bytes: int | None = field(init=False)
    status_message: str = field(init=False)

    def __post_init__(self) -> None:
        mailbox = self.mailbox
        object.__setattr__(
            self,
            "mailbox_status",
            mailbox.mailbox_status if mailbox is not None else MailboxQuotaStatus.UNKNOWN,
        )
        object.__setattr__(
            self,
            "send_status",
            mailbox.send_status if mailbox is not None else MailboxOperationStatus.UNKNOWN,
        )
        object.__setattr__(
            self,
            "receive_status",
            mailbox.receive_status if mailbox is not None else MailboxOperationStatus.UNKNOWN,
        )
        object.__setattr__(
            self,
            "send_threshold_percent",
            mailbox.send_threshold_percent if mailbox is not None else None,
        )
        object.__setattr__(
            self,
            "over_send_limit_bytes",
            mailbox.over_send_limit_bytes if mailbox is not None else None,
        )
        object.__setattr__(
            self,
            "remaining_to_send_limit_bytes",
            mailbox.remaining_to_send_limit_bytes if mailbox is not None else None,
        )
        object.__setattr__(
            self,
            "status_message",
            mailbox.status_message
            if mailbox is not None
            else "Mailbox storage information is unavailable.",
        )

    @property
    def exact(self) -> bool:
        return self.status is QuotaStatus.EXACT

    @property
    def has_useful_information(self) -> bool:
        return self.mailbox is not None

    @property
    def effective_capacity(self) -> QuotaValue:
        """Expose an evidenced total only; legacy generic fallbacks are not capacity."""

        if self.mailbox is not None and self.mailbox.total_capacity.known:
            return self.mailbox.total_capacity
        return QuotaValue(None)

    @property
    def capacity_is_fallback(self) -> bool:
        return False


class QuotaGateway(Protocol):
    """Return server values or a clearly sourced estimate; return ``None`` if unavailable."""

    def get_quota(self) -> QuotaInfo | None: ...


class QuotaService:
    """Combine server data, user settings, notification evidence, and a narrow fallback."""

    def __init__(
        self,
        gateway: QuotaGateway,
        *,
        observations: Sequence[QuotaObservation] = (),
        user_overrides: QuotaOverrides | None = None,
        automatic_detection: bool = True,
        prefer_server_values: bool = True,
        use_guc_fallback: bool = False,
        fallback_capacity_bytes: int | None = DEFAULT_FALLBACK_CAPACITY_BYTES,
    ) -> None:
        self.gateway = gateway
        self.observations = tuple(observations)
        self.user_overrides = user_overrides or QuotaOverrides()
        self.automatic_detection = automatic_detection
        self.prefer_server_values = prefer_server_values
        self.use_guc_fallback = use_guc_fallback
        if fallback_capacity_bytes is not None and fallback_capacity_bytes <= 0:
            raise ValueError("The configured fallback capacity must be positive.")
        # Retain the keyword for callers of the previous API, but never promote an
        # unevidenced generic number to an Exchange capacity or receive threshold.
        self.fallback_capacity = QuotaValue(None)

    def report(
        self,
        *,
        local_paths: Mapping[str, Path] | Sequence[Path] = (),
    ) -> QuotaReport:
        reason: str | None = None
        try:
            server_quota = self.gateway.get_quota()
        except UnsupportedFeatureError as exc:
            server_quota = None
            reason = str(exc) or "The server did not expose mailbox quota values through EWS."

        if server_quota is not None:
            self._validate_quota(server_quota)
            if _is_legacy_estimated_quota_pair(server_quota):
                return QuotaReport(
                    status=QuotaStatus.UNAVAILABLE,
                    mailbox=None,
                    unavailable_reason=(
                        "Only an estimated folder sum was available; it is not presented "
                        "as an Exchange mailbox quota."
                    ),
                    local_disks=self.local_disk_usage(local_paths),
                    fallback_capacity=self.fallback_capacity,
                )
            server_quota = self._normalize_gateway_sources(server_quota)

        active: QuotaInfo | None = self._merge(server_quota)
        if active is not None and not _has_useful_mailbox_value(active):
            active = None
        status = self._report_status(active)
        if reason is None and status is not QuotaStatus.EXACT:
            reason = _partial_reason(active)

        return QuotaReport(
            status=status,
            mailbox=active,
            unavailable_reason=reason,
            local_disks=self.local_disk_usage(local_paths),
            observations=self.observations if self.automatic_detection else (),
            fallback_capacity=self.fallback_capacity,
        )

    def _merge(self, server_quota: QuotaInfo | None) -> QuotaInfo:
        base = server_quota or QuotaInfo()
        observations = self.observations if self.automatic_detection else ()
        no_user_values = all(
            value is None
            for value in (
                self.user_overrides.issue_warning_bytes,
                self.user_overrides.prohibit_send_bytes,
                self.user_overrides.prohibit_send_receive_bytes,
            )
        )
        if not observations and no_user_values and not self.use_guc_fallback:
            return base
        issue = self._active_threshold(
            base.issue_warning,
            _best_observation(observations, QuotaThresholdKind.ISSUE_WARNING),
            self.user_overrides.issue_warning_bytes,
        )
        prohibit_send = self._active_threshold(
            base.prohibit_send,
            _best_observation(observations, QuotaThresholdKind.PROHIBIT_SEND),
            self.user_overrides.prohibit_send_bytes,
            fallback=(
                QuotaValue(
                    KNOWN_GUC_PROHIBIT_SEND_BYTES,
                    QuotaSource.KNOWN_GUC_FALLBACK,
                    QuotaConfidence.MEDIUM,
                    detail="Observed GUC Exchange send-block threshold; not total capacity.",
                )
                if self.use_guc_fallback
                else None
            ),
        )
        prohibit_send_receive = self._active_threshold(
            base.prohibit_send_receive,
            _best_observation(observations, QuotaThresholdKind.PROHIBIT_SEND_RECEIVE),
            self.user_overrides.prohibit_send_receive_bytes,
        )

        limitations = list(base.limitations)
        if base.used_bytes is None:
            limitations.append("Current server-side mailbox usage is unavailable.")
        elif base.current_usage.source is QuotaSource.ESTIMATED:
            limitations.append(
                "Usage is estimated from visible item sizes; hidden or recoverable data may differ."
            )
        if prohibit_send_receive.value_bytes is None:
            limitations.append("The Exchange send-and-receive block threshold is unknown.")
        if base.quota_bytes is None:
            limitations.append("No total mailbox capacity has been reported by Exchange.")

        return replace(
            base,
            warning_bytes=issue.value_bytes,
            issue_warning_source=issue.source,
            issue_warning_confidence=issue.confidence,
            prohibit_send_bytes=prohibit_send.value_bytes,
            prohibit_send_source=prohibit_send.source,
            prohibit_send_confidence=prohibit_send.confidence,
            prohibit_send_receive_bytes=prohibit_send_receive.value_bytes,
            prohibit_send_receive_source=prohibit_send_receive.source,
            prohibit_send_receive_confidence=prohibit_send_receive.confidence,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    def _active_threshold(
        self,
        server: QuotaValue,
        observation: QuotaObservation | None,
        user_value: int | None,
        *,
        fallback: QuotaValue | None = None,
    ) -> QuotaValue:
        observed = (
            QuotaValue(
                observation.threshold_bytes,
                observation.source,
                observation.confidence,
                observation.observed_at,
                observation.evidence,
            )
            if observation is not None
            else None
        )
        configured = (
            QuotaValue(
                user_value,
                QuotaSource.USER_CONFIGURED,
                QuotaConfidence.HIGH,
                detail="Explicit user override.",
            )
            if user_value is not None
            else None
        )

        server_is_authoritative = server.known and server.source in {
            QuotaSource.SERVER_REPORTED,
            QuotaSource.AUTOMATICALLY_DETECTED,
        }
        if configured is not None and not (self.prefer_server_values and server_is_authoritative):
            return configured
        candidates = [candidate for candidate in (server, observed, fallback) if candidate]
        if configured is not None:
            candidates.append(configured)
        known = [candidate for candidate in candidates if candidate.known]
        if not known:
            return QuotaValue(None)
        return max(known, key=_value_precedence)

    @staticmethod
    def _normalize_gateway_sources(quota: QuotaInfo) -> QuotaInfo:
        legacy_source = quota.current_usage.source

        def source_for(value: int | None, explicit: QuotaSource) -> QuotaSource:
            if value is None:
                return QuotaSource.UNKNOWN
            if explicit is not QuotaSource.UNKNOWN:
                return explicit
            if legacy_source is not QuotaSource.UNKNOWN:
                # Keep the compatibility field authoritative so callers comparing the
                # immutable first-pass model do not observe an unnecessary rewrite.
                return explicit
            return QuotaSource.SERVER_REPORTED

        return replace(
            quota,
            usage_source=source_for(quota.used_bytes, quota.usage_source),
            total_capacity_source=source_for(
                quota.quota_bytes,
                quota.total_capacity_source,
            ),
            issue_warning_source=source_for(
                quota.warning_bytes,
                quota.issue_warning_source,
            ),
            prohibit_send_source=source_for(
                quota.prohibit_send_bytes,
                quota.prohibit_send_source,
            ),
            prohibit_send_receive_source=source_for(
                quota.prohibit_send_receive_bytes,
                quota.prohibit_send_receive_source,
            ),
        )

    @staticmethod
    def _report_status(quota: QuotaInfo | None) -> QuotaStatus:
        if quota is None:
            return QuotaStatus.UNAVAILABLE
        if quota.current_usage.source is QuotaSource.ESTIMATED:
            return QuotaStatus.ESTIMATED
        total_is_exact = (
            quota.used_bytes is not None
            and quota.quota_bytes is not None
            and quota.current_usage.confidence is QuotaConfidence.EXACT
            and quota.total_capacity.confidence is QuotaConfidence.EXACT
        )
        all_thresholds_exact = quota.used_bytes is not None and all(
            value.known and value.confidence is QuotaConfidence.EXACT
            for value in (
                quota.issue_warning,
                quota.prohibit_send,
                quota.prohibit_send_receive,
            )
        )
        return QuotaStatus.EXACT if total_is_exact or all_thresholds_exact else QuotaStatus.PARTIAL

    @staticmethod
    def local_disk_usage(
        paths: Mapping[str, Path] | Sequence[Path],
    ) -> tuple[LocalDiskUsage, ...]:
        if isinstance(paths, Mapping):
            candidates = [(str(label), Path(path)) for label, path in paths.items()]
        else:
            candidates = [(str(path), Path(path)) for path in paths]
        reports: list[LocalDiskUsage] = []
        seen: set[tuple[str, Path]] = set()
        for label, raw in candidates:
            path = raw.expanduser().resolve(strict=False)
            key = (label, path)
            if key in seen:
                continue
            seen.add(key)
            existing = path
            while not existing.exists() and existing != existing.parent:
                existing = existing.parent
            try:
                usage = shutil.disk_usage(existing)
            except OSError as exc:
                reports.append(
                    LocalDiskUsage(
                        label=label,
                        path=path,
                        available=False,
                        error=f"Disk capacity unavailable ({type(exc).__name__}).",
                    )
                )
            else:
                reports.append(
                    LocalDiskUsage(
                        label=label,
                        path=path,
                        available=True,
                        total_bytes=usage.total,
                        used_bytes=usage.used,
                        free_bytes=usage.free,
                    )
                )
        return tuple(reports)

    @staticmethod
    def _validate_quota(quota: QuotaInfo) -> None:
        fields = (
            quota.used_bytes,
            quota.quota_bytes,
            quota.warning_bytes,
            quota.prohibit_send_bytes,
            quota.prohibit_send_receive_bytes,
            quota.deleted_bytes,
            quota.recoverable_bytes,
            quota.archive_used_bytes,
            quota.archive_quota_bytes,
        )
        if any(value is not None and value < 0 for value in fields):
            raise ConfigurationError("Mailbox quota values may not be negative.")
        thresholds = (
            quota.quota_bytes,
            quota.warning_bytes,
            quota.prohibit_send_bytes,
            quota.prohibit_send_receive_bytes,
            quota.archive_quota_bytes,
        )
        if any(value == 0 for value in thresholds):
            raise ConfigurationError("Known mailbox quota thresholds must be positive.")


SettingReader = Callable[[str, object], object]


def quota_service_from_settings(
    gateway: QuotaGateway,
    setting: SettingReader,
) -> QuotaService:
    """Build the quota service without mislabelling a documented fallback as an override.

    Threshold values are user overrides only when their companion provenance setting says
    ``user_configured``.  The shipped 92 MiB GUC value therefore remains an explicitly
    labelled send-block fallback, never a fabricated total mailbox capacity.
    """

    def source(setting_id: str) -> str:
        return str(setting(setting_id, "unknown")).strip().casefold().replace("-", "_")

    def configured_threshold(value_id: str, source_id: str) -> int | None:
        if source(source_id) != QuotaSource.USER_CONFIGURED.value:
            return None
        raw = setting(value_id, None)
        if raw is None:
            return None
        try:
            amount = Decimal(str(raw))
        except InvalidOperation as exc:
            raise ConfigurationError(f"{value_id} must be a positive number of MiB.") from exc
        if not amount.is_finite() or amount <= 0:
            raise ConfigurationError(f"{value_id} must be a positive number of MiB.")
        return int(amount * MEBIBYTE)

    return QuotaService(
        gateway,
        user_overrides=QuotaOverrides(
            issue_warning_bytes=configured_threshold(
                "storage.issue_warning_mb",
                "storage.issue_warning_source",
            ),
            prohibit_send_bytes=configured_threshold(
                "storage.prohibit_send_mb",
                "storage.prohibit_send_source",
            ),
            prohibit_send_receive_bytes=configured_threshold(
                "storage.prohibit_send_receive_mb",
                "storage.prohibit_send_receive_source",
            ),
        ),
        automatic_detection=_setting_bool(
            setting("storage.quota_auto_detect", True),
            "storage.quota_auto_detect",
        ),
        prefer_server_values=_setting_bool(
            setting("storage.prefer_server_quota", True),
            "storage.prefer_server_quota",
        ),
        use_guc_fallback=(
            source("storage.prohibit_send_source") == QuotaSource.KNOWN_GUC_FALLBACK.value
        ),
    )


def compact_quota_status(report: QuotaReport) -> str:
    """Return a truthful one-line status suitable for the persistent TUI header."""

    mailbox = report.mailbox
    if mailbox is None:
        return "Storage unavailable"
    limit = mailbox.prohibit_send_bytes or report.effective_capacity.value_bytes
    used = "?" if mailbox.used_bytes is None else _quota_amount(mailbox.used_bytes)
    if mailbox.used_bytes is not None and mailbox.current_usage.source is QuotaSource.ESTIMATED:
        used = f"~{used}"
    if limit is not None:
        label = "Send limit" if mailbox.prohibit_send_bytes is not None else "Storage"
        return f"{label} {used}/{_quota_amount(limit)}"
    if mailbox.used_bytes is not None:
        return f"Storage {used}/?"
    return "Storage unavailable"


def _setting_bool(value: object, setting_id: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{setting_id} must be true or false.")


class QuotaNoticeParser:
    """Conservatively parse high-signal Exchange quota-warning notices."""

    _number = r"(?P<number>\d[\d\s.,]*\d|\d)\s*(?P<unit>GiB|MiB|KiB|GB|MB|KB|bytes?|B)"
    _current_labels = (
        r"current(?:\s+mailbox)?\s+size",
        r"mailbox\s+size\s+(?:at\s+(?:the\s+)?(?:time|notification))",
        r"aktuelle\s+gr(?:ö|o)(?:ß|ss)e",
        r"aktuelle\s+postfachgr(?:ö|o)(?:ß|ss)e",
    )
    _warning_labels = (
        r"issue\s*warning(?:\s*quota|\s*threshold)?",
        r"warning\s+(?:quota|threshold|limit)",
        r"warnschwelle",
    )
    _send_labels = (
        r"prohibit\s*send(?:\s*quota|\s*threshold)?",
        r"send(?:ing)?\s+(?:quota|threshold|limit)",
        r"maximum\s+size",
        r"maximale\s+gr(?:ö|o)(?:ß|ss)e",
    )
    _send_receive_labels = (
        r"prohibit\s*send\s*(?:and|&|/)\s*receive(?:\s*quota|\s*threshold)?",
        r"send\s*(?:and|&|/)\s*receive\s+(?:quota|threshold|limit)",
        r"sende?[-\s]*(?:und|&)\s*empfangssperre",
    )

    @classmethod
    def parse(
        cls,
        *,
        subject: str,
        body: str,
        sender: str | None = None,
        headers: Mapping[str, str] | None = None,
        observed_at: datetime | None = None,
    ) -> QuotaObservation | None:
        clean_subject = cls._clean(subject, limit=2_000)
        clean_body = cls._clean(body, limit=100_000)
        combined = f"{clean_subject}\n{clean_body}".casefold()
        event = cls._classify_event(combined)
        if event is None or not cls._looks_system_generated(
            clean_subject,
            sender,
            headers or {},
        ):
            return None

        current = cls._extract_value(combined, cls._current_labels)
        threshold_kind = {
            QuotaEvent.WARNING: QuotaThresholdKind.ISSUE_WARNING,
            QuotaEvent.PROHIBIT_SEND: QuotaThresholdKind.PROHIBIT_SEND,
            QuotaEvent.PROHIBIT_SEND_RECEIVE: QuotaThresholdKind.PROHIBIT_SEND_RECEIVE,
        }[event]
        labels = {
            QuotaThresholdKind.ISSUE_WARNING: cls._warning_labels,
            QuotaThresholdKind.PROHIBIT_SEND: cls._send_labels,
            QuotaThresholdKind.PROHIBIT_SEND_RECEIVE: cls._send_receive_labels,
        }[threshold_kind]
        threshold = cls._extract_value(combined, labels)
        if current is None or threshold is None or threshold <= 0:
            return None
        if event is not QuotaEvent.WARNING and current < threshold * 0.98:
            # Rounding can put a warning just below its displayed threshold, but a large
            # mismatch is strong evidence this is ordinary prose rather than a quota event.
            return None

        trusted_sender = cls._trusted_sender(sender)
        strong_subject = cls._quota_subject(clean_subject)
        confidence = (
            QuotaConfidence.HIGH if trusted_sender and strong_subject else QuotaConfidence.MEDIUM
        )
        return QuotaObservation(
            event=event,
            threshold_kind=threshold_kind,
            historical_usage_bytes=current,
            threshold_bytes=threshold,
            confidence=confidence,
            observed_at=observed_at,
        )

    @staticmethod
    def _clean(value: str, *, limit: int) -> str:
        bounded = value[:limit]
        without_tags = re.sub(r"<[^>]{0,500}>", " ", bounded)
        unescaped = html.unescape(without_tags).replace("\xa0", " ")
        return re.sub(r"\s+", " ", unescaped).strip()

    @classmethod
    def _classify_event(cls, text: str) -> QuotaEvent | None:
        send_receive = (
            r"(?:can(?:not|'t)|no\s+longer\s+(?:able\s+to\s+)?)\s*"
            r"(?:send|receive)(?:\s+messages?)?\s*(?:or|and|&)\s*(?:receive|send)",
            r"sending\s+and\s+receiving\s+(?:is|are)\s+(?:blocked|disabled)",
            r"weder\s+nachrichten\s+senden\s+noch\s+empfangen",
        )
        prohibit_send = (
            r"(?:can(?:not|'t)|can\s+no\s+longer|no\s+longer\s+(?:able\s+to\s+)?)\s*"
            r"send(?:\s+(?:new\s+)?messages?)?",
            r"sending\s+(?:is|has\s+been)\s+(?:blocked|disabled|prohibited)",
            r"(?:kann|können)\s+keine\s+nachrichten\s+mehr\s+senden",
            r"senden\s+von\s+nachrichten\s+(?:ist\s+)?nicht\s+mehr\s+möglich",
        )
        warning = (
            r"mailbox\s+(?:is\s+)?(?:almost|nearly)\s+full",
            r"mailbox\s+(?:is\s+)?approaching\s+(?:its\s+)?(?:quota|limit)",
            r"quota\s+warning",
            r"postfach\s+(?:ist\s+)?(?:fast|nahezu)\s+voll",
        )
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in send_receive):
            return QuotaEvent.PROHIBIT_SEND_RECEIVE
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in prohibit_send):
            return QuotaEvent.PROHIBIT_SEND
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in warning):
            return QuotaEvent.WARNING
        return None

    @classmethod
    def _extract_value(cls, text: str, labels: Sequence[str]) -> int | None:
        candidates: list[tuple[int, int]] = []
        for label in labels:
            before = rf"{cls._number}\s*(?:,|;|-)?\s*(?P<label>{label})"
            match = re.search(before, text, re.IGNORECASE)
            if match:
                parsed = _parse_size(match.group("number"), match.group("unit"))
                if parsed is not None:
                    candidates.append((2, parsed))
            after = rf"(?P<label>{label})\s*(?:is|was|:|=|-)?\s*{cls._number}"
            match = re.search(after, text, re.IGNORECASE)
            if match:
                parsed = _parse_size(match.group("number"), match.group("unit"))
                if parsed is not None:
                    separator = text[match.end("label") : match.start("number")]
                    explicit_separator = bool(
                        re.search(r"[:=]|\b(?:is|was)\b", separator, re.IGNORECASE)
                    )
                    candidates.append((4 if explicit_separator else 1, parsed))
        return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None

    @classmethod
    def _looks_system_generated(
        cls,
        subject: str,
        sender: str | None,
        headers: Mapping[str, str],
    ) -> bool:
        auto_submitted = next(
            (
                str(value).casefold()
                for key, value in headers.items()
                if key.casefold() == "auto-submitted"
            ),
            "",
        )
        return cls._trusted_sender(sender) or (
            cls._quota_subject(subject) and auto_submitted.startswith("auto-")
        )

    @staticmethod
    def _trusted_sender(sender: str | None) -> bool:
        raw = (sender or "").strip()
        if re.sub(r"\s+", " ", raw).casefold() in {
            "microsoft exchange",
            "system attendant",
        }:
            return True
        display_name, address = parseaddr(raw)
        local_part = address.rsplit("@", 1)[0].casefold() if "@" in address else ""
        normalized_name = re.sub(r"\s+", " ", display_name).strip().casefold()
        return local_part in {"postmaster", "systemattendant", "system-attendant"} or (
            normalized_name in {"microsoft exchange", "system attendant"}
        )

    @staticmethod
    def _quota_subject(subject: str) -> bool:
        value = subject.casefold()
        patterns = (
            r"(?:mailbox|postfach).*(?:full|voll|quota|storage|size|limit)",
            r"(?:quota|storage).*(?:warning|limit|threshold)",
            r"(?:warning|warnung).*(?:mailbox|postfach|quota)",
        )
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def parse_quota_notification(
    *,
    subject: str,
    body: str,
    sender: str | None = None,
    headers: Mapping[str, str] | None = None,
    observed_at: datetime | None = None,
) -> QuotaObservation | None:
    """Convenience wrapper around :class:`QuotaNoticeParser`."""

    return QuotaNoticeParser.parse(
        subject=subject,
        body=body,
        sender=sender,
        headers=headers,
        observed_at=observed_at,
    )


def render_quota_report(report: QuotaReport, *, monochrome: bool = False) -> str:
    """Render independently sourced thresholds and evidenced capacity only."""

    quota = report.mailbox
    if quota is None:
        reason = report.unavailable_reason or "no usable server or configured values"
        capacity = report.effective_capacity
        if capacity.known:
            lines = [
                f"Mailbox usage unavailable (Quota unavailable: {reason})",
                f"Configured fallback capacity: {_quota_amount(capacity.value_bytes)} "
                "(not server-reported)",
            ]
        else:
            lines = [f"Mailbox storage unavailable (Quota unavailable: {reason})"]
    else:
        capacity = report.effective_capacity
        capacity_percent = (
            max(quota.used_bytes / capacity.value_bytes * 100, 0.0)
            if quota.used_bytes is not None and capacity.value_bytes
            else None
        )
        used = quota.used_bytes
        send_limit = quota.prohibit_send_bytes
        send_percent = quota.send_threshold_percent
        if send_percent is not None and used is not None and send_limit is not None:
            width = 10
            filled = min(width, max(0, round((send_percent / 100) * width)))
            full, empty = ("#", "-") if monochrome else ("\N{FULL BLOCK}", "\N{LIGHT SHADE}")
            bar = full * filled + empty * (width - filled)
            suffix = (
                " · SEND BLOCKED" if quota.send_status is MailboxOperationStatus.BLOCKED else ""
            )
            lines = [
                f"Send quota [{bar}] {send_percent:.2f}%  "
                f"{_quota_amount(used)}/{_quota_amount(send_limit)}{suffix}"
            ]
        elif used is not None:
            lines = [f"Mailbox {_quota_amount(used)} used · Exchange limits unknown"]
        elif send_limit is not None:
            lines = [f"Send limit {_quota_amount(send_limit)} · usage unavailable"]
        else:
            lines = ["Mailbox storage unavailable"]

        if capacity_percent is not None and used is not None and capacity.value_bytes is not None:
            width = 10
            filled = min(width, max(0, round((capacity_percent / 100) * width)))
            full, empty = ("#", "-") if monochrome else ("\N{FULL BLOCK}", "\N{LIGHT SHADE}")
            bar = full * filled + empty * (width - filled)
            capacity_label = (
                "configured fallback; not server-reported"
                if report.capacity_is_fallback
                else "server-reported capacity"
            )
            lines.extend(
                [
                    f"Capacity [{bar}] {capacity_percent:.2f}%  "
                    f"{_quota_amount(used)}/{_quota_amount(capacity.value_bytes)}",
                    f"Capacity source: {capacity_label}",
                    f"Remaining: {_quota_amount(max(capacity.value_bytes - used, 0))}",
                ]
            )

        lines.extend(
            (
                f"Current mailbox usage: {_quota_amount(quota.used_bytes)} "
                f"(source: {_source_label(quota.current_usage.source)}, "
                f"confidence: {quota.current_usage.confidence.value})",
                _threshold_line("Issue Warning", quota.issue_warning),
                _threshold_line("Prohibit Send", quota.prohibit_send),
                _threshold_line("Prohibit Send & Receive", quota.prohibit_send_receive),
                f"Status: {quota.mailbox_status.value.replace('_', ' ').upper()}",
                f"Sending: {quota.send_status.value.replace('_', ' ')}",
                f"Receiving: {quota.receive_status.value.replace('_', ' ')}",
                quota.status_message,
            )
        )
        if quota.over_send_limit_bytes:
            lines.append(f"Amount over send limit: {_quota_amount(quota.over_send_limit_bytes)}")
    for disk in report.local_disks:
        if disk.available:
            lines.append(
                f"Local disk ({disk.label}): {_human_bytes(disk.free_bytes)} free of "
                f"{_human_bytes(disk.total_bytes)}"
            )
        else:
            lines.append(f"Local disk ({disk.label}): unavailable")
    return "\n".join(lines)


def _best_observation(
    observations: Sequence[QuotaObservation],
    kind: QuotaThresholdKind,
) -> QuotaObservation | None:
    matching = [item for item in observations if item.threshold_kind is kind]
    if not matching:
        return None

    def rank(item: QuotaObservation) -> tuple[int, float]:
        confidence = {
            QuotaConfidence.EXACT: 4,
            QuotaConfidence.HIGH: 3,
            QuotaConfidence.MEDIUM: 2,
            QuotaConfidence.LOW: 1,
            QuotaConfidence.UNKNOWN: 0,
        }[item.confidence]
        observed = item.observed_at.timestamp() if item.observed_at is not None else 0.0
        return confidence, observed

    return max(matching, key=rank)


def _value_precedence(value: QuotaValue) -> tuple[int, int, float]:
    source_rank = {
        QuotaSource.SERVER_REPORTED: 7,
        QuotaSource.AUTOMATICALLY_DETECTED: 6,
        QuotaSource.USER_CONFIGURED: 5,
        QuotaSource.SERVER_NOTIFICATION: 4,
        QuotaSource.KNOWN_GUC_FALLBACK: 3,
        QuotaSource.ESTIMATED: 2,
        QuotaSource.UNKNOWN: 1,
    }[value.source]
    confidence_rank = {
        QuotaConfidence.EXACT: 4,
        QuotaConfidence.HIGH: 3,
        QuotaConfidence.MEDIUM: 2,
        QuotaConfidence.LOW: 1,
        QuotaConfidence.UNKNOWN: 0,
    }[value.confidence]
    observed = value.observed_at.timestamp() if value.observed_at is not None else 0.0
    return source_rank, confidence_rank, observed


def _has_useful_mailbox_value(quota: QuotaInfo) -> bool:
    return (
        any(
            value is not None
            for value in (
                quota.used_bytes,
                quota.quota_bytes,
                quota.warning_bytes,
                quota.prohibit_send_bytes,
                quota.prohibit_send_receive_bytes,
                quota.deleted_bytes,
                quota.recoverable_bytes,
                quota.archive_used_bytes,
                quota.archive_quota_bytes,
            )
        )
        or quota.observed_send_blocked
        or quota.observed_receive_blocked
    )


def _is_legacy_estimated_quota_pair(quota: QuotaInfo) -> bool:
    source = (quota.source or "").casefold()
    return (
        quota.quota_bytes is not None
        and quota.usage_source is QuotaSource.UNKNOWN
        and any(token in source for token in ("folder_sum", "estimate", "approx"))
    )


def _partial_reason(quota: QuotaInfo | None) -> str:
    if quota is None:
        return "Mailbox usage and Exchange quota thresholds are unavailable."
    if quota.current_usage.source is QuotaSource.ESTIMATED:
        return "Current mailbox usage is an estimate, not an exact Exchange statistic."
    if quota.used_bytes is None:
        return "Current mailbox usage is unavailable; one or more threshold values are known."
    return "Some Exchange quota thresholds are unavailable or are not server-reported."


def _parse_size(number: str, unit: str) -> int | None:
    normalized = number.replace("\xa0", "").replace(" ", "")
    if "," in normalized and "." in normalized:
        decimal_separator = "," if normalized.rfind(",") > normalized.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        normalized = normalized.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif normalized.count(",") > 1:
        normalized = normalized.replace(",", "")
    elif normalized.count(".") > 1:
        normalized = normalized.replace(".", "")
    elif "," in normalized:
        before, after = normalized.split(",", 1)
        normalized = before + after if len(after) == 3 else f"{before}.{after}"
    elif "." in normalized:
        before, after = normalized.split(".", 1)
        if len(after) == 3:
            normalized = before + after
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    factors = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": MEBIBYTE,
        "mib": MEBIBYTE,
        "gb": 1024 * MEBIBYTE,
        "gib": 1024 * MEBIBYTE,
    }
    factor = factors.get(unit.casefold())
    if factor is None:
        return None
    result = amount * factor
    if result < 0:
        return None
    return int(result.to_integral_value())


def _threshold_line(label: str, value: QuotaValue) -> str:
    if not value.known:
        return f"{label}: Unknown (source: Unknown, confidence: unknown)"
    return (
        f"{label}: {_quota_amount(value.value_bytes)} "
        f"(source: {_source_label(value.source)}, confidence: {value.confidence.value})"
    )


def _source_label(source: QuotaSource) -> str:
    label = source.value.replace("_", " ").title()
    return label.replace("Guc", "GUC")


def _quota_amount(value: int | None) -> str:
    if value is None:
        return "Unknown"
    if value >= MEBIBYTE:
        amount = value / MEBIBYTE
        if amount.is_integer():
            return f"{int(amount)} MiB"
        return f"{amount:.1f} MiB"
    return _human_bytes(value)


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


__all__ = [
    "KNOWN_GUC_PROHIBIT_SEND_BYTES",
    "LocalDiskUsage",
    "QuotaEvent",
    "QuotaGateway",
    "QuotaNoticeParser",
    "QuotaObservation",
    "QuotaOverrides",
    "QuotaReport",
    "QuotaService",
    "QuotaStatus",
    "compact_quota_status",
    "parse_quota_notification",
    "quota_service_from_settings",
    "render_quota_report",
]
