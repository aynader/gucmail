"""The whole path a new message takes to a Windows toast: profile privacy, then the
provider's privacy, then the toast text. Every combination is checked."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gucmail.models import Address, NotificationEvent
from gucmail.notification_events import EventDisclosurePolicy, PrivacyMode
from gucmail.notification_providers.windows import WindowsToastSettings, build_windows_toast
from gucmail.services.profiled_notifications import disclosed_event

SENDER = Address(name="Dr. Example", email="example@guc.edu.eg")


def _event(subject: str | None) -> NotificationEvent:
    return NotificationEvent(
        event_id="evt-1",
        event_type="new_message",
        occurred_at=datetime(2026, 11, 13, 9, 0, tzinfo=UTC),
        account_id="me@student.guc.edu.eg",
        folder="Inbox",
        message_reference="m1",
        sender=SENDER,
        subject=subject,
        preview="Please find the slides attached.",
    )


def _toast(subject: str | None, profile: PrivacyMode, provider: PrivacyMode) -> tuple[str, str]:
    event = disclosed_event(_event(subject), EventDisclosurePolicy(mode=profile))
    request = build_windows_toast(event, WindowsToastSettings(privacy=provider))
    return request.title, request.body


@pytest.mark.parametrize("profile", list(PrivacyMode))
@pytest.mark.parametrize("provider", list(PrivacyMode))
def test_no_subject_only_when_the_subject_is_really_empty(profile, provider):
    _title, body = _toast("Lecture 5", profile, provider)
    assert "(No subject)" not in body
    _title, body = _toast("", profile, provider)
    both_allow_subject = {profile, provider} <= {PrivacyMode.SENDER_AND_SUBJECT, PrivacyMode.FULL}
    assert ("(No subject)" in body) == both_allow_subject


@pytest.mark.parametrize("profile", list(PrivacyMode))
@pytest.mark.parametrize("provider", list(PrivacyMode))
def test_the_stricter_setting_always_wins(profile, provider):
    _title, body = _toast("Lecture 5", profile, provider)
    order = list(PrivacyMode)
    shows_subject = min(order.index(profile), order.index(provider)) >= order.index(
        PrivacyMode.SENDER_AND_SUBJECT
    )
    assert ("Lecture 5" in body) == shows_subject
    assert ("slides" in body) == (profile is provider is PrivacyMode.FULL)


def test_sender_and_subject_toast_reads_naturally():
    title, body = _toast(
        "Lecture 5", PrivacyMode.SENDER_AND_SUBJECT, PrivacyMode.SENDER_AND_SUBJECT
    )
    assert (title, body) == ("Dr. Example", "Lecture 5")


def test_private_toast_says_only_new_message():
    title, body = _toast("Lecture 5", PrivacyMode.PRIVATE, PrivacyMode.FULL)
    assert (title, body) == ("GUC Mail", "You have a new message.")


def test_settings_saved_before_1_2_still_load():
    assert PrivacyMode("sender_subject") is PrivacyMode.SENDER_AND_SUBJECT


def test_every_privacy_level_has_a_plain_label():
    labels = [mode.label for mode in PrivacyMode]
    assert len(set(labels)) == len(labels)
    assert all("_" not in label for label in labels)
