"""Reviewed automatic-reply settings with stale-preview protection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
    ConflictError,
    ErrorContext,
    ExitCode,
    GUCmailError,
)


class AutomaticReplyState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    SCHEDULED = "scheduled"


class ExternalReplyAudience(StrEnum):
    NONE = "none"
    KNOWN = "known"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class AutomaticReplySettings:
    state: AutomaticReplyState = AutomaticReplyState.DISABLED
    external_audience: ExternalReplyAudience = ExternalReplyAudience.NONE
    internal_reply: str = ""
    external_reply: str = ""
    start: datetime | None = None
    end: datetime | None = None

    @property
    def fingerprint(self) -> str:
        # Exchange ignores replies while disabled and dates while continuously enabled.
        # Readback may contain today's date even though it is not part of that policy.
        values: dict[str, object] = {"state": self.state.value}
        if self.state is not AutomaticReplyState.DISABLED:
            values.update(
                external_audience=self.external_audience.value,
                internal_reply=self.internal_reply,
            )
            if self.external_audience is not ExternalReplyAudience.NONE:
                values["external_reply"] = self.external_reply
        if self.state is AutomaticReplyState.SCHEDULED:
            values.update(
                start=self.start.astimezone(UTC).isoformat() if self.start else None,
                end=self.end.astimezone(UTC).isoformat() if self.end else None,
            )
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AutomaticReplyPlan:
    before: AutomaticReplySettings
    after: AutomaticReplySettings


class AutomaticReplyGateway(Protocol):
    def get_automatic_replies(self) -> AutomaticReplySettings: ...
    def set_automatic_replies(self, settings: AutomaticReplySettings) -> None: ...


class AutomaticReplyVerificationError(GUCmailError):
    exit_code = ExitCode.PARTIAL


def parse_reply_time(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError("Use an ISO date and time with an explicit UTC offset.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ConfigurationError("Automatic-reply times need an explicit UTC offset.")
    return result


def validate_automatic_replies(
    settings: AutomaticReplySettings, *, now: datetime | None = None
) -> AutomaticReplySettings:
    try:
        state = AutomaticReplyState(settings.state)
        audience = ExternalReplyAudience(settings.external_audience)
    except ValueError as exc:
        raise ConfigurationError(
            "Choose a valid automatic-reply state and external audience."
        ) from exc
    for body in (settings.internal_reply, settings.external_reply):
        if len(body.encode("utf-8")) > 128 * 1024:
            raise ConfigurationError("Each automatic reply is limited to 128 KiB.")
    if state is not AutomaticReplyState.DISABLED:
        if not settings.internal_reply.strip():
            raise ConfigurationError("Enter an internal automatic reply before enabling it.")
        if audience is not ExternalReplyAudience.NONE and not settings.external_reply.strip():
            raise ConfigurationError("Enter the external automatic reply for this audience.")
    if state is AutomaticReplyState.SCHEDULED:
        if not settings.start or not settings.end:
            raise ConfigurationError("Scheduled automatic replies need a start and an end.")
        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (settings.start, settings.end)
        ):
            raise ConfigurationError("Automatic-reply times need an explicit UTC offset.")
        if settings.end.astimezone(UTC) <= settings.start.astimezone(UTC) or (
            settings.end.astimezone(UTC) <= (now or datetime.now(UTC))
        ):
            raise ConfigurationError("End must follow start and be in the future.")
    return replace(settings, state=state, external_audience=audience)


class AutomaticReplyService:
    def __init__(self, gateway: AutomaticReplyGateway) -> None:
        self.gateway = gateway

    def get(self) -> AutomaticReplySettings:
        return self.gateway.get_automatic_replies()

    def plan(
        self, settings: AutomaticReplySettings, *, current: AutomaticReplySettings | None = None
    ) -> AutomaticReplyPlan:
        after = validate_automatic_replies(settings)
        return AutomaticReplyPlan(self.get() if current is None else current, after)

    def apply(self, plan: AutomaticReplyPlan, *, confirmed: bool = False) -> AutomaticReplySettings:
        if not confirmed:
            raise ConfirmationRequiredError("Review and confirm the exact automatic-reply policy.")
        validate_automatic_replies(plan.after)
        if self.get().fingerprint != plan.before.fingerprint:
            raise ConflictError("Automatic replies changed after preview. Refresh before applying.")
        self.gateway.set_automatic_replies(plan.after)
        # Do not replay a successful/uncertain write if this readback fails.
        try:
            result = self.get()
        except Exception as exc:
            raise AutomaticReplyVerificationError(
                "Exchange accepted the automatic-reply change, but readback failed. "
                "Refresh the policy before applying another change.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            ) from exc
        if result.fingerprint != plan.after.fingerprint:
            raise AutomaticReplyVerificationError(
                "Exchange accepted the change, but the returned policy differs from the preview. "
                "Refresh and review the current policy before applying another change.",
                context=ErrorContext(retry_safe=False, mutation_uncertain=True),
            )
        return result
