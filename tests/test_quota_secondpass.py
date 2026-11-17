from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gucmail.errors import ConfigurationError
from gucmail.models import (
    MailboxOperationStatus,
    MailboxQuotaStatus,
    QuotaConfidence,
    QuotaInfo,
    QuotaSource,
    QuotaThresholdKind,
    QuotaValue,
    to_primitive,
)
from gucmail.services.quota import (
    KNOWN_GUC_PROHIBIT_SEND_BYTES,
    QuotaEvent,
    QuotaObservation,
    QuotaOverrides,
    QuotaService,
    QuotaStatus,
    compact_quota_status,
    parse_quota_notification,
    quota_service_from_settings,
    render_quota_report,
)

MIB = 1024 * 1024


class QuotaGatewayFake:
    def __init__(self, quota: QuotaInfo | None) -> None:
        self.quota = quota

    def get_quota(self) -> QuotaInfo | None:
        return self.quota


def quota_at(used_mb: int) -> QuotaInfo:
    return QuotaInfo(
        used_bytes=used_mb * MIB,
        warning_bytes=70 * MIB,
        prohibit_send_bytes=92 * MIB,
        prohibit_send_receive_bytes=110 * MIB,
        usage_source=QuotaSource.SERVER_REPORTED,
        issue_warning_source=QuotaSource.SERVER_REPORTED,
        prohibit_send_source=QuotaSource.SERVER_REPORTED,
        prohibit_send_receive_source=QuotaSource.SERVER_REPORTED,
    )


@pytest.mark.parametrize(
    ("used_mb", "status", "send", "receive"),
    (
        (
            50,
            MailboxQuotaStatus.NORMAL,
            MailboxOperationStatus.ALLOWED,
            MailboxOperationStatus.ALLOWED,
        ),
        (
            75,
            MailboxQuotaStatus.WARNING,
            MailboxOperationStatus.ALLOWED,
            MailboxOperationStatus.ALLOWED,
        ),
        (
            85,
            MailboxQuotaStatus.NEAR_SEND_LIMIT,
            MailboxOperationStatus.ALLOWED,
            MailboxOperationStatus.ALLOWED,
        ),
        (
            92,
            MailboxQuotaStatus.SEND_BLOCKED,
            MailboxOperationStatus.BLOCKED,
            MailboxOperationStatus.ALLOWED,
        ),
        (
            110,
            MailboxQuotaStatus.SEND_RECEIVE_BLOCKED,
            MailboxOperationStatus.BLOCKED,
            MailboxOperationStatus.BLOCKED,
        ),
    ),
)
def test_independent_exchange_thresholds_derive_operational_bands(
    used_mb: int,
    status: MailboxQuotaStatus,
    send: MailboxOperationStatus,
    receive: MailboxOperationStatus,
) -> None:
    quota = quota_at(used_mb)

    assert quota.mailbox_status is status
    assert quota.send_status is send
    assert quota.receive_status is receive


def test_97_over_92_regression_is_send_threshold_not_capacity() -> None:
    quota = QuotaInfo(
        used_bytes=97 * MIB,
        prohibit_send_bytes=92 * MIB,
        usage_source=QuotaSource.SERVER_REPORTED,
        usage_confidence=QuotaConfidence.EXACT,
        prohibit_send_source=QuotaSource.SERVER_NOTIFICATION,
        prohibit_send_confidence=QuotaConfidence.HIGH,
    )

    assert quota.send_threshold_percent == pytest.approx(105.4347826087)
    assert quota.send_threshold_percent is not None and quota.send_threshold_percent > 100
    assert quota.quota_bytes is None
    assert quota.total_capacity.value_bytes is None
    assert quota.prohibit_send_receive_bytes is None
    assert quota.mailbox_status is MailboxQuotaStatus.SEND_BLOCKED
    assert quota.send_status is MailboxOperationStatus.BLOCKED
    assert quota.receive_status is MailboxOperationStatus.NOT_KNOWN_TO_BE_BLOCKED
    assert quota.over_send_limit_bytes == 5 * MIB
    assert quota.remaining_to_send_limit_bytes == 0
    assert "No receive block has been detected" in quota.status_message


def test_percentages_and_headroom_are_not_mathematically_capped() -> None:
    quota = QuotaInfo(
        used_bytes=120 * MIB,
        quota_bytes=100 * MIB,
        prohibit_send_bytes=92 * MIB,
    )

    assert quota.percent_used == 120
    assert quota.send_threshold_percent == pytest.approx(130.4347826087)
    assert quota.remaining_bytes == 0
    assert quota.send_headroom_bytes == -28 * MIB
    assert quota.over_send_limit_bytes == 28 * MIB
    assert quota.remaining_to_warning_bytes is None
    assert quota.remaining_to_receive_limit_bytes is None


def test_known_guc_fallback_populates_only_prohibit_send_when_enabled() -> None:
    report = QuotaService(
        QuotaGatewayFake(None),
        use_guc_fallback=True,
    ).report()

    assert report.status is QuotaStatus.PARTIAL
    assert report.mailbox is not None
    assert report.mailbox.used_bytes is None
    assert report.mailbox.quota_bytes is None
    assert report.mailbox.warning_bytes is None
    assert report.mailbox.prohibit_send_bytes == KNOWN_GUC_PROHIBIT_SEND_BYTES
    assert report.mailbox.prohibit_send_source is QuotaSource.KNOWN_GUC_FALLBACK
    assert report.mailbox.prohibit_send_confidence is QuotaConfidence.MEDIUM
    assert report.mailbox.prohibit_send_receive_bytes is None
    assert report.mailbox.mailbox_status is MailboxQuotaStatus.UNKNOWN
    assert "Send limit 92 MiB" in render_quota_report(report, monochrome=True)


def test_fallback_is_never_a_generic_100_mb_capacity() -> None:
    mailbox = (
        QuotaService(
            QuotaGatewayFake(None),
            use_guc_fallback=True,
        )
        .report()
        .mailbox
    )

    assert mailbox is not None
    primitive = to_primitive(mailbox)
    assert primitive["quota_bytes"] is None
    assert primitive["warning_bytes"] is None
    assert primitive["prohibit_send_receive_bytes"] is None
    assert primitive["prohibit_send_bytes"] == 92 * MIB


def test_screenshot_shaped_notice_parses_historical_usage_and_send_threshold() -> None:
    observed_at = datetime(2026, 8, 6, 17, 2, tzinfo=UTC)
    observation = parse_quota_notification(
        subject="Microsoft Exchange mailbox quota warning",
        sender="Microsoft Exchange <postmaster@example.edu>",
        body=("97 MB Current size 92 MB Maximum size Your mailbox can no longer send messages."),
        observed_at=observed_at,
    )

    assert observation is not None
    assert observation.event is QuotaEvent.PROHIBIT_SEND
    assert observation.threshold_kind is QuotaThresholdKind.PROHIBIT_SEND
    assert observation.historical_usage_bytes == 97 * MIB
    assert observation.threshold_bytes == 92 * MIB
    assert observation.observed_at == observed_at
    assert observation.source is QuotaSource.SERVER_NOTIFICATION
    assert observation.confidence is QuotaConfidence.HIGH


@pytest.mark.parametrize(
    ("subject", "sender", "body", "event", "current", "threshold"),
    (
        (
            "Mailbox quota warning",
            "postmaster@example.edu",
            "Current size: 97 MB; Maximum size: 92 MB. Your mailbox cannot send messages.",
            QuotaEvent.PROHIBIT_SEND,
            97 * MIB,
            92 * MIB,
        ),
        (
            "Mailbox storage limit",
            "Microsoft Exchange",
            (
                "Current mailbox size is 101,711,872 bytes. "
                "Prohibit send quota is 96,468,992 bytes. "
                "Sending is blocked."
            ),
            QuotaEvent.PROHIBIT_SEND,
            97 * MIB,
            92 * MIB,
        ),
        (
            "Postfach Warnung",
            "postmaster@example.edu",
            (
                "Aktuelle Größe: 97 MB; Maximale Größe: 92 MB. "
                "Ihr Postfach kann keine Nachrichten mehr senden."
            ),
            QuotaEvent.PROHIBIT_SEND,
            97 * MIB,
            92 * MIB,
        ),
        (
            "Mailbox almost full",
            "postmaster@example.edu",
            ("Current size: 80 MB; Issue Warning quota: 80 MB. Your mailbox is almost full."),
            QuotaEvent.WARNING,
            80 * MIB,
            80 * MIB,
        ),
        (
            "Mailbox full - quota limit",
            "postmaster@example.edu",
            (
                "Current size: 110 MB; Prohibit Send & Receive quota: 110 MB. "
                "You cannot send or receive messages."
            ),
            QuotaEvent.PROHIBIT_SEND_RECEIVE,
            110 * MIB,
            110 * MIB,
        ),
    ),
)
def test_notification_parser_supports_structured_and_localized_formats(
    subject: str,
    sender: str,
    body: str,
    event: QuotaEvent,
    current: int,
    threshold: int,
) -> None:
    parsed = parse_quota_notification(subject=subject, sender=sender, body=body)

    assert parsed is not None
    assert parsed.event is event
    assert parsed.historical_usage_bytes == current
    assert parsed.threshold_bytes == threshold


@pytest.mark.parametrize(
    ("subject", "sender", "body"),
    (
        (
            "Weekly storage report",
            "colleague@example.edu",
            "Current size: 97 MB. Maximum size: 92 MB. The quoted UI says cannot send.",
        ),
        (
            "Mailbox quota warning",
            "postmaster@example.edu",
            "Current size: 97 MB. Maximum size: 92 MB.",
        ),
        (
            "Mailbox quota warning",
            "postmaster@example.edu",
            "Current size: 20 MB. Maximum size: 92 MB. Your mailbox cannot send messages.",
        ),
        (
            "Mailbox quota warning",
            "postmaster@example.edu",
            "Your mailbox cannot send messages, but no structured sizes are present.",
        ),
    ),
)
def test_notification_parser_rejects_low_signal_or_inconsistent_prose(
    subject: str,
    sender: str,
    body: str,
) -> None:
    assert parse_quota_notification(subject=subject, sender=sender, body=body) is None


def test_notification_parser_does_not_trust_arbitrary_noreply_or_subject_alone() -> None:
    body = "Current size: 97 MB; Maximum size: 92 MB. Your mailbox can no longer send messages."

    assert (
        parse_quota_notification(
            subject="Mailbox quota warning",
            sender="noreply@unrelated.example",
            body=body,
        )
        is None
    )
    assert (
        parse_quota_notification(
            subject="Mailbox quota warning",
            sender="colleague@example.edu",
            body=body,
        )
        is None
    )
    assert (
        parse_quota_notification(
            subject="Mailbox quota warning",
            sender="colleague@example.edu",
            headers={"Auto-Submitted": "auto-generated"},
            body=body,
        )
        is not None
    )


def test_quota_observation_rejects_mismatched_kind_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="do not match"):
        QuotaObservation(
            event=QuotaEvent.PROHIBIT_SEND,
            threshold_kind=QuotaThresholdKind.ISSUE_WARNING,
            historical_usage_bytes=97 * MIB,
            threshold_bytes=92 * MIB,
        )
    with pytest.raises(ValueError, match="time zone"):
        QuotaObservation(
            event=QuotaEvent.PROHIBIT_SEND,
            threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
            historical_usage_bytes=97 * MIB,
            threshold_bytes=92 * MIB,
            observed_at=datetime(2026, 8, 6, 17, 2),
        )


def test_historical_notification_never_becomes_current_usage() -> None:
    observation = QuotaObservation(
        event=QuotaEvent.PROHIBIT_SEND,
        threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
        historical_usage_bytes=97 * MIB,
        threshold_bytes=92 * MIB,
        observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    current = QuotaInfo(
        used_bytes=70 * MIB,
        usage_source=QuotaSource.SERVER_REPORTED,
    )

    report = QuotaService(
        QuotaGatewayFake(current),
        observations=(observation,),
    ).report()

    assert report.mailbox is not None
    assert report.mailbox.used_bytes == 70 * MIB
    assert report.mailbox.prohibit_send_bytes == 92 * MIB
    assert report.mailbox.prohibit_send_source is QuotaSource.SERVER_NOTIFICATION
    assert report.mailbox.mailbox_status is MailboxQuotaStatus.NORMAL
    assert report.observations[0].historical_usage_bytes == 97 * MIB


def test_server_values_win_by_default_and_user_can_explicitly_override() -> None:
    server = QuotaInfo(
        prohibit_send_bytes=100 * MIB,
        prohibit_send_source=QuotaSource.SERVER_REPORTED,
    )
    observed = QuotaObservation(
        event=QuotaEvent.PROHIBIT_SEND,
        threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
        historical_usage_bytes=97 * MIB,
        threshold_bytes=92 * MIB,
    )
    overrides = QuotaOverrides(prohibit_send_bytes=95 * MIB)

    preferred = QuotaService(
        QuotaGatewayFake(server),
        observations=(observed,),
        user_overrides=overrides,
        use_guc_fallback=True,
    ).report()
    overridden = QuotaService(
        QuotaGatewayFake(server),
        observations=(observed,),
        user_overrides=overrides,
        prefer_server_values=False,
        use_guc_fallback=True,
    ).report()

    assert preferred.mailbox is not None
    assert preferred.mailbox.prohibit_send_bytes == 100 * MIB
    assert preferred.mailbox.prohibit_send_source is QuotaSource.SERVER_REPORTED
    assert overridden.mailbox is not None
    assert overridden.mailbox.prohibit_send_bytes == 95 * MIB
    assert overridden.mailbox.prohibit_send_source is QuotaSource.USER_CONFIGURED


def test_legacy_source_names_map_to_precise_provenance_without_rewriting() -> None:
    notification = QuotaInfo(
        prohibit_send_bytes=92 * MIB,
        source="server_notification",
    )
    estimate = QuotaInfo(used_bytes=74 * MIB, source="folder_sum_estimate")

    assert notification.prohibit_send.source is QuotaSource.SERVER_NOTIFICATION
    assert notification.prohibit_send.confidence is QuotaConfidence.HIGH
    assert estimate.current_usage.source is QuotaSource.ESTIMATED
    assert estimate.current_usage.confidence is QuotaConfidence.LOW


def test_user_override_beats_notification_and_notification_beats_fallback() -> None:
    observed = QuotaObservation(
        event=QuotaEvent.PROHIBIT_SEND,
        threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
        historical_usage_bytes=97 * MIB,
        threshold_bytes=92 * MIB,
    )
    notification = QuotaService(
        QuotaGatewayFake(None),
        observations=(observed,),
        use_guc_fallback=True,
    ).report()
    configured = QuotaService(
        QuotaGatewayFake(None),
        observations=(observed,),
        user_overrides=QuotaOverrides(prohibit_send_bytes=95 * MIB),
        use_guc_fallback=True,
    ).report()

    assert notification.mailbox is not None
    assert notification.mailbox.prohibit_send_source is QuotaSource.SERVER_NOTIFICATION
    assert configured.mailbox is not None
    assert configured.mailbox.prohibit_send_bytes == 95 * MIB
    assert configured.mailbox.prohibit_send_source is QuotaSource.USER_CONFIGURED


def test_disabling_automatic_detection_ignores_notification_evidence() -> None:
    observed = QuotaObservation(
        event=QuotaEvent.PROHIBIT_SEND,
        threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
        historical_usage_bytes=99 * MIB,
        threshold_bytes=95 * MIB,
    )
    report = QuotaService(
        QuotaGatewayFake(None),
        observations=(observed,),
        automatic_detection=False,
        use_guc_fallback=True,
    ).report()

    assert report.mailbox is not None
    assert report.mailbox.prohibit_send_bytes == 92 * MIB
    assert report.mailbox.prohibit_send_source is QuotaSource.KNOWN_GUC_FALLBACK
    assert report.observations == ()


def test_explicit_usage_estimate_remains_useful_but_is_clearly_labeled() -> None:
    estimate = QuotaInfo(
        used_bytes=74 * MIB,
        usage_source=QuotaSource.ESTIMATED,
        usage_confidence=QuotaConfidence.LOW,
    )
    report = QuotaService(QuotaGatewayFake(estimate)).report()

    assert report.status is QuotaStatus.ESTIMATED
    assert report.mailbox is not None
    assert report.mailbox.quota_bytes is None
    assert "limits unknown" in render_quota_report(report)
    assert "estimate" in (report.unavailable_reason or "").casefold()


def test_rendering_exposes_send_semantics_sources_and_monochrome_state() -> None:
    report = QuotaService(
        QuotaGatewayFake(
            QuotaInfo(
                used_bytes=97 * MIB,
                usage_source=QuotaSource.SERVER_REPORTED,
            )
        ),
        observations=(
            QuotaObservation(
                event=QuotaEvent.PROHIBIT_SEND,
                threshold_kind=QuotaThresholdKind.PROHIBIT_SEND,
                historical_usage_bytes=97 * MIB,
                threshold_bytes=92 * MIB,
            ),
        ),
    ).report()

    rendered = render_quota_report(report, monochrome=True)
    assert "Send quota [##########] 105.43%  97 MiB/92 MiB · SEND BLOCKED" in rendered
    assert "Prohibit Send: 92 MiB (source: Server Notification" in rendered
    assert "Prohibit Send & Receive: Unknown" in rendered
    assert "Receiving: not known to be blocked" in rendered
    assert "total capacity" not in rendered.casefold()


def test_report_serialization_includes_derived_send_and_receive_states() -> None:
    report = QuotaService(
        QuotaGatewayFake(
            QuotaInfo(
                used_bytes=97 * MIB,
                prohibit_send_bytes=92 * MIB,
                usage_source=QuotaSource.SERVER_REPORTED,
                prohibit_send_source=QuotaSource.SERVER_NOTIFICATION,
            )
        )
    ).report()

    primitive = to_primitive(report)
    assert primitive["send_threshold_percent"] == pytest.approx(105.4347826087)
    assert primitive["mailbox_status"] == "send_blocked"
    assert primitive["send_status"] == "blocked"
    assert primitive["receive_status"] == "not_known_to_be_blocked"
    assert primitive["mailbox"]["quota_bytes"] is None


def test_quota_validation_rejects_negative_or_zero_thresholds() -> None:
    with pytest.raises(ValueError):
        QuotaValue(-1, QuotaSource.USER_CONFIGURED)
    with pytest.raises(ValueError):
        QuotaOverrides(prohibit_send_bytes=0)
    with pytest.raises(ConfigurationError):
        QuotaService(QuotaGatewayFake(QuotaInfo(prohibit_send_bytes=0))).report()
    with pytest.raises(ConfigurationError):
        QuotaService(QuotaGatewayFake(QuotaInfo(used_bytes=-1))).report()


def test_local_storage_stays_separate_from_server_mailbox_storage(tmp_path: Path) -> None:
    report = QuotaService(
        QuotaGatewayFake(None),
        use_guc_fallback=True,
    ).report(local_paths={"cache": Path(tmp_path)})

    assert report.local_disks[0].available
    rendered = render_quota_report(report, monochrome=True)
    assert "Send limit 92 MiB" in rendered
    assert "Local disk (cache):" in rendered


def test_settings_factory_preserves_known_guc_fallback_provenance() -> None:
    values: dict[str, object] = {
        "storage.prohibit_send_mb": 92.0,
        "storage.prohibit_send_source": "known_guc_fallback",
        "storage.quota_auto_detect": True,
        "storage.prefer_server_quota": True,
    }
    service = quota_service_from_settings(
        QuotaGatewayFake(None),
        lambda setting_id, fallback: values.get(setting_id, fallback),
    )

    report = service.report()

    assert report.mailbox is not None
    assert report.mailbox.prohibit_send_bytes == KNOWN_GUC_PROHIBIT_SEND_BYTES
    assert report.mailbox.prohibit_send_source is QuotaSource.KNOWN_GUC_FALLBACK
    assert compact_quota_status(report) == "Mailbox limit 92 MiB"


def test_compact_quota_shows_actual_usage_and_marks_estimates() -> None:
    exact = QuotaService(QuotaGatewayFake(quota_at(73))).report()
    estimate = QuotaService(
        QuotaGatewayFake(
            QuotaInfo(
                used_bytes=74 * MIB,
                prohibit_send_bytes=92 * MIB,
                usage_source=QuotaSource.ESTIMATED,
                usage_confidence=QuotaConfidence.LOW,
            )
        )
    ).report()
    assert compact_quota_status(exact) == "Mailbox 73 MiB of 92 MiB (79%)"
    assert compact_quota_status(estimate) == "Mailbox ~74 MiB of 92 MiB (80%)"


def test_settings_factory_uses_numeric_threshold_only_for_user_override() -> None:
    values: dict[str, object] = {
        "storage.prohibit_send_mb": "95.5",
        "storage.prohibit_send_source": "user_configured",
        "storage.quota_auto_detect": "false",
        "storage.prefer_server_quota": "false",
    }
    report = quota_service_from_settings(
        QuotaGatewayFake(None),
        lambda setting_id, fallback: values.get(setting_id, fallback),
    ).report()

    assert report.mailbox is not None
    assert report.mailbox.prohibit_send_bytes == int(95.5 * MIB)
    assert report.mailbox.prohibit_send_source is QuotaSource.USER_CONFIGURED


def test_settings_factory_rejects_invalid_user_override() -> None:
    values: dict[str, object] = {
        "storage.prohibit_send_mb": "not-a-number",
        "storage.prohibit_send_source": "user_configured",
    }

    with pytest.raises(ConfigurationError, match=r"storage\.prohibit_send_mb"):
        quota_service_from_settings(
            QuotaGatewayFake(None),
            lambda setting_id, fallback: values.get(setting_id, fallback),
        )
