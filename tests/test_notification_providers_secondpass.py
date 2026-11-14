from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gucmail.models import Address, Importance, NotificationEvent, to_primitive
from gucmail.notification_providers import (
    PROVIDER_CATALOG,
    PROVIDER_TEMPLATES,
    CircuitBreakerPolicy,
    CircuitPhase,
    CircuitState,
    CompatibleWebhookConfig,
    DestinationError,
    DiscordAdapter,
    DiscordConfig,
    GenericWebhookAdapter,
    GenericWebhookConfig,
    NDJSONAdapter,
    PayloadPolicy,
    PreparedRequestNotificationProvider,
    PrivacyMode,
    ProviderIsolationDispatcher,
    ProviderKind,
    RedirectPolicy,
    RetryContext,
    RetryDisposition,
    RetryPolicy,
    SlackAdapter,
    TeamsAdapter,
    TelegramAdapter,
    TelegramConfig,
    TransportOutcome,
    WindowsToastProvider,
    WindowsToastSettings,
    bounded_json,
    build_toast_burst,
    build_windows_toast,
    circuit_decision,
    event_payload,
    make_click_token,
    mask_discord_webhook_url,
    mask_secret_url,
    mask_telegram_token,
    parse_click_token,
    provider_templates,
    record_circuit_result,
    schedule_retry,
    sign_payload,
    stable_idempotency_key,
    validate_destination,
    validate_discord_webhook_url,
    validate_redirect,
    verify_payload_signature,
)
from gucmail.services.notifications import DeliveryResult, DeliveryState

NOW = datetime(2026, 8, 9, 9, 15, 12, tzinfo=UTC)
DISCORD_TOKEN = "synthetic_Discord-token_abcdefghijklmnopqrstuvwxyz012345"
DISCORD_URL = f"https://discord.com/api/webhooks/123456789012345/{DISCORD_TOKEN}"
TELEGRAM_TOKEN = "123456789:synthetic_bot_token_ABCDEFGHIJKLMN"


@pytest.fixture
def hostile_event() -> NotificationEvent:
    return NotificationEvent(
        event_id="evt-hostile-123",
        event_type="new_message",
        occurred_at=NOW,
        account_id="student@guc.example",
        folder="Inbox\x1b[2J",
        message_reference="msg-local-123",
        sender=Address("external@example.test", "@everyone <@user> Sender\x1b[31m"),
        subject="@here <@&role> token=topsecret Important update",
        importance=Importance.HIGH,
        has_attachments=True,
        preview="Preview for private.person@example.test " + ("very long " * 300),
        open_hint="gucmail messages show msg-local-123",
    )


def test_provider_catalog_is_complete_unique_and_self_describing() -> None:
    assert {item.provider_id for item in PROVIDER_CATALOG} == set(ProviderKind)
    assert len(PROVIDER_CATALOG) == len({item.provider_id for item in PROVIDER_CATALOG})
    for provider in PROVIDER_CATALOG:
        assert provider.display_name
        assert provider.description
        assert provider.capabilities
        assert len(provider.settings) == len({setting.setting_id for setting in provider.settings})
        for setting in provider.settings:
            if setting.setting_id in {"url", "webhook_url", "bot_token", "hmac_secret"}:
                assert setting.secret

    assert len(PROVIDER_TEMPLATES) == len({item.template_id for item in PROVIDER_TEMPLATES})
    assert {item.provider_id for item in PROVIDER_TEMPLATES} == set(ProviderKind)
    assert {item.name for item in provider_templates(ProviderKind.DISCORD)} == {
        "Discord embed",
        "Private Discord alert",
    }


@pytest.mark.parametrize(
    ("mode", "present", "absent"),
    [
        (PrivacyMode.FULL, {"sender", "subject", "preview"}, set()),
        (PrivacyMode.SENDER_AND_SUBJECT, {"sender", "subject"}, {"preview"}),
        (PrivacyMode.SENDER_ONLY, {"sender"}, {"subject", "preview", "folder"}),
        (PrivacyMode.PRIVATE, {"event", "event_id"}, {"sender", "subject", "folder"}),
        (
            PrivacyMode.METADATA_ONLY,
            {"folder", "importance", "has_attachments"},
            {"sender", "subject", "preview"},
        ),
    ],
)
def test_privacy_presets_fail_closed(
    hostile_event: NotificationEvent,
    mode: PrivacyMode,
    present: set[str],
    absent: set[str],
) -> None:
    payload = event_payload(hostile_event, PayloadPolicy(privacy=mode))
    assert present <= payload.keys()
    assert not (absent & payload.keys())


def test_field_allowlist_only_reduces_privacy_and_redacts(hostile_event: NotificationEvent) -> None:
    policy = PayloadPolicy(
        privacy=PrivacyMode.FULL,
        field_allowlist=frozenset({"event_id", "sender", "subject", "not-real"}) - {"not-real"},
        expose_external_addresses=True,
    )
    payload = event_payload(hostile_event, policy)
    assert payload.keys() == {"event_id", "sender", "subject"}
    assert "\x1b" not in json.dumps(payload)
    assert "topsecret" not in str(payload)
    assert "external@example.test" not in str(payload)
    assert "<redacted>@example.test" in str(payload)
    hidden = event_payload(hostile_event, replace(policy, expose_external_addresses=False))
    assert hidden["sender"] == {"name": "@everyone <@user> Sender", "address": None}


def test_payload_policy_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Unknown payload"):
        PayloadPolicy(field_allowlist=frozenset({"future-private-body"}))


def test_stable_idempotency_key_is_private_deterministic_and_provider_scoped(
    hostile_event: NotificationEvent,
) -> None:
    first = stable_idempotency_key(hostile_event)
    assert first == stable_idempotency_key(replace(hostile_event))
    assert first != stable_idempotency_key(hostile_event, "discord")
    assert hostile_event.account_id not in first
    assert hostile_event.event_id not in first
    assert first.startswith("gucmail-v1-")


def test_bounded_json_truncates_unicode_on_boundaries() -> None:
    payload = {
        "event": "new_message",
        "event_id": "evt-1",
        "received_at": NOW.isoformat(),
        "preview": "\U0001f600" * 2_000,
    }
    rendered = bounded_json(payload, 256)
    decoded = json.loads(rendered)
    assert len(rendered) <= 256
    assert decoded["event_id"] == "evt-1"
    assert decoded["preview"].endswith("...")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/hook",
        "https://user:password@example.test/hook",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://example.test:99999/hook",
        "https://example.test/hook\nInjected",
    ],
)
def test_destination_validation_rejects_unsafe_defaults(url: str) -> None:
    with pytest.raises(DestinationError):
        validate_destination(url)


def test_destination_validation_allows_explicit_local_http_with_warnings() -> None:
    with pytest.raises(DestinationError, match="Plain HTTP"):
        validate_destination("http://192.168.1.9:8123/mail")
    result = validate_destination(
        "http://192.168.1.9:8123/mail",
        allow_http=True,
        allow_private=True,
    )
    assert result.secure_transport is False
    assert result.local_network is True
    assert len(result.warnings) == 2


def test_redirect_policy_blocks_by_default_and_prevents_tls_downgrade() -> None:
    with pytest.raises(DestinationError, match="limit"):
        validate_redirect("https://hooks.example.test/a", "/b", redirect_count=0)
    accepted = validate_redirect(
        "https://hooks.example.test/a",
        "/b",
        redirect_count=0,
        policy=RedirectPolicy(maximum_redirects=2),
    )
    assert accepted.url == "https://hooks.example.test/b"
    with pytest.raises(DestinationError, match="HTTPS-to-HTTP"):
        validate_redirect(
            "https://hooks.example.test/a",
            "http://hooks.example.test/b",
            redirect_count=0,
            policy=RedirectPolicy(maximum_redirects=2),
        )
    downgraded = validate_redirect(
        "https://hooks.example.test/a",
        "http://hooks.example.test/b",
        redirect_count=0,
        policy=RedirectPolicy(maximum_redirects=2, allow_https_downgrade=True),
    )
    assert downgraded.secure_transport is False


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/12345/abcdefghijklmnopqrstuvwxyz",
        "https://example.test/api/webhooks/12345/abcdefghijklmnopqrstuvwxyz",
        "https://discord.com/channels/123/456",
        "https://discord.com/api/webhooks/not-numeric/abcdefghijklmnopqrstuvwxyz",
        "https://discord.com/api/webhooks/12345/short",
        f"{DISCORD_URL}?wait=true",
    ],
)
def test_discord_url_validation_is_strict(url: str) -> None:
    with pytest.raises(DestinationError):
        validate_discord_webhook_url(url)


def test_discord_secret_is_masked_and_never_repr_exposed() -> None:
    config = DiscordConfig(DISCORD_URL)
    assert DISCORD_TOKEN not in repr(config)
    assert DISCORD_TOKEN not in config.masked_url
    assert DISCORD_TOKEN not in mask_discord_webhook_url(DISCORD_URL)
    assert "discord.com/api/webhooks" in config.masked_url
    assert mask_discord_webhook_url("not a URL") == "<invalid Discord webhook>"


@pytest.mark.parametrize("embeds", [True, False])
def test_discord_payload_is_bounded_and_mentions_are_disabled(
    hostile_event: NotificationEvent,
    embeds: bool,
) -> None:
    request = DiscordAdapter(
        DiscordConfig(
            DISCORD_URL,
            policy=PayloadPolicy(
                privacy=PrivacyMode.FULL,
                redact_email_addresses=False,
                expose_external_addresses=True,
                max_payload_bytes=8_000,
            ),
            embeds=embeds,
        )
    ).prepare(hostile_event)
    rendered = request.body.decode()
    payload = request.json()
    assert len(request.body) <= 8_000
    assert payload["allowed_mentions"] == {
        "parse": [],
        "users": [],
        "roles": [],
        "replied_user": False,
    }
    for dangerous in ("@everyone", "@here", "<@user>", "<@&role>"):
        assert dangerous not in rendered
    assert request.headers["Idempotency-Key"] == stable_idempotency_key(hostile_event, "discord")
    assert request.follow_redirects is False


def test_discord_private_mode_does_not_disclose_message_fields(
    hostile_event: NotificationEvent,
) -> None:
    request = DiscordAdapter(
        DiscordConfig(DISCORD_URL, policy=PayloadPolicy(privacy=PrivacyMode.PRIVATE))
    ).prepare(hostile_event)
    rendered = request.body.decode()
    assert "Important update" not in rendered
    assert "Sender" not in rendered
    assert "Inbox" not in rendered


def test_slack_and_teams_shapes_keep_provider_assumptions_isolated(
    hostile_event: NotificationEvent,
) -> None:
    config = CompatibleWebhookConfig(
        "https://hooks.example.test/incoming",
        PayloadPolicy(
            privacy=PrivacyMode.FULL,
            redact_email_addresses=False,
            expose_external_addresses=True,
        ),
    )
    slack = SlackAdapter(config).prepare(hostile_event).json()
    assert slack["blocks"][0]["text"]["type"] == "plain_text"
    assert "@everyone" not in slack["text"]
    teams = TeamsAdapter(config).prepare(hostile_event).json()
    assert teams["type"] == "message"
    attachment = teams["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"


def test_telegram_adapter_uses_plain_text_and_masks_bot_secret(
    hostile_event: NotificationEvent,
) -> None:
    config = TelegramConfig(TELEGRAM_TOKEN, "-100123456789")
    request = TelegramAdapter(config).prepare(hostile_event)
    assert TELEGRAM_TOKEN not in repr(config)
    assert TELEGRAM_TOKEN not in mask_telegram_token(TELEGRAM_TOKEN)
    assert request.url.endswith(f"bot{TELEGRAM_TOKEN}/sendMessage")
    payload = request.json()
    assert "parse_mode" not in payload
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["chat_id"] == "-100123456789"


def test_generic_webhook_is_allowlisted_idempotent_and_signed(
    hostile_event: NotificationEvent,
) -> None:
    secret = b"0123456789abcdef"
    config = GenericWebhookConfig(
        "automation",
        "https://hooks.example.test/gucmail?existing=yes",
        method="PATCH",
        headers={"X-Synthetic": "fixture"},
        query={"source": "gucmail"},
        policy=PayloadPolicy(
            privacy=PrivacyMode.FULL,
            field_allowlist=frozenset({"event", "event_id", "subject"}),
            redact_email_addresses=False,
        ),
        hmac_secret=secret,
    )
    request = GenericWebhookAdapter(config).prepare(hostile_event, delivery_time=NOW)
    assert request.method == "PATCH"
    assert request.json().keys() == {"event", "event_id", "subject"}
    assert "existing=yes&source=gucmail" in request.url
    timestamp = str(int(NOW.timestamp()))
    expected = (
        "sha256="
        + hmac.new(
            secret,
            timestamp.encode() + b"." + request.body,
            hashlib.sha256,
        ).hexdigest()
    )
    assert request.headers["X-GUCMail-Signature"] == expected
    assert request.headers["X-Synthetic"] == "fixture"
    assert verify_payload_signature(secret, request.body, timestamp, expected)
    assert not verify_payload_signature(secret, request.body + b"x", timestamp, expected)
    assert not verify_payload_signature(b"wrong-wrong-wrong!", request.body, timestamp, expected)


def test_hmac_known_vector_and_validation() -> None:
    secret = b"0123456789abcdef"
    payload, timestamp = b'{"event":"test"}', "1786266912"
    expected = (
        "sha256="
        + hmac.new(
            secret,
            timestamp.encode() + b"." + payload,
            hashlib.sha256,
        ).hexdigest()
    )
    assert sign_payload(secret, payload, timestamp) == expected
    with pytest.raises(ValueError, match="at least 16"):
        sign_payload(b"short", payload, timestamp)


def test_generic_webhook_refuses_tls_disable_and_hides_secret_repr() -> None:
    url = "https://hooks.example.test/very-secret-token"
    with pytest.raises(ValueError, match="may not be disabled"):
        GenericWebhookConfig("bad", url, verify_tls=False)
    config = GenericWebhookConfig("safe", url, hmac_secret=b"0123456789abcdef")
    assert url not in repr(config)
    assert b"0123456789abcdef".decode() not in repr(config)
    assert "very-secret-token" not in mask_secret_url(url)


def test_prepared_request_compatibility_provider_uses_injected_transport(
    hostile_event: NotificationEvent,
) -> None:
    class Transport:
        def __init__(self) -> None:
            self.requests = []

        def deliver(self, request):
            self.requests.append(request)
            return TransportOutcome(True, status_code=202, attempts=2)

    transport = Transport()
    builder = DiscordAdapter(DiscordConfig(DISCORD_URL))
    provider = PreparedRequestNotificationProvider(
        builder,
        transport,
        name="discord:student-affairs",
    )
    result = provider.notify(hostile_event)
    assert result == DeliveryResult(
        hostile_event.event_id,
        "discord:student-affairs",
        DeliveryState.DELIVERED,
        attempts=2,
        status_code=202,
    )
    assert len(transport.requests) == 1
    assert transport.requests[0].provider is ProviderKind.DISCORD

    disabled = PreparedRequestNotificationProvider(builder, transport, enabled=False)
    assert disabled.notify(hostile_event).state is DeliveryState.DISABLED
    assert len(transport.requests) == 1


def test_ndjson_adapter_outputs_one_bounded_line(hostile_event: NotificationEvent) -> None:
    line = NDJSONAdapter(
        PayloadPolicy(
            privacy=PrivacyMode.FULL,
            field_allowlist=frozenset({"event_id", "subject", "preview"}),
            max_payload_bytes=512,
        )
    ).prepare(hostile_event)
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert len(line) <= 513
    assert json.loads(line).keys() == {"event_id", "preview", "subject"}


@pytest.mark.parametrize(
    ("privacy", "contains", "omits"),
    [
        (PrivacyMode.FULL, ("Sender", "Important update", "Preview"), ()),
        (PrivacyMode.SENDER_AND_SUBJECT, ("Sender", "Important update"), ("Preview",)),
        (PrivacyMode.SENDER_ONLY, ("Sender",), ("Important update", "Preview")),
        (PrivacyMode.PRIVATE, ("new message",), ("Sender", "Important update", "Preview")),
    ],
)
def test_windows_toast_privacy_modes(
    hostile_event: NotificationEvent,
    privacy: PrivacyMode,
    contains: tuple[str, ...],
    omits: tuple[str, ...],
) -> None:
    request = build_windows_toast(
        hostile_event,
        WindowsToastSettings(privacy=privacy),
    )
    rendered = request.title + "\n" + request.body
    for value in contains:
        assert value in rendered
    for value in omits:
        assert value not in rendered
    assert "\x1b" not in rendered
    assert request.activation is not None
    assert request.activation.arguments == ("messages", "show", "msg-local-123")


def test_windows_click_token_is_canonical_and_shell_free() -> None:
    activation = make_click_token("msg-local-safe")
    assert parse_click_token(activation.token) == activation
    assert activation.arguments == ("messages", "show", "msg-local-safe")
    with pytest.raises(ValueError):
        make_click_token("unsafe reference & del")
    with pytest.raises(ValueError):
        parse_click_token(activation.token + "tamper")


def test_windows_burst_is_capped_and_groups_remainder(
    hostile_event: NotificationEvent,
) -> None:
    events = [replace(hostile_event, event_id=f"evt-{index}") for index in range(8)]
    requests = build_toast_burst(
        events,
        WindowsToastSettings(maximum_notifications_per_burst=3),
    )
    assert len(requests) == 3
    assert requests[-1].body == "6 additional new messages"
    assert requests[-1].constituent_event_ids == tuple(f"evt-{index}" for index in range(2, 8))


def test_windows_backend_protocol_and_disabled_state(hostile_event: NotificationEvent) -> None:
    class Backend:
        def __init__(self) -> None:
            self.requests = []

        def show(self, request) -> None:
            self.requests.append(request)

    backend = Backend()
    delivered = WindowsToastProvider(backend).notify(hostile_event)
    assert delivered.state is DeliveryState.DELIVERED
    assert len(backend.requests) == 1
    disabled = WindowsToastProvider(
        backend,
        WindowsToastSettings(enabled=False),
    ).notify(hostile_event)
    assert disabled.state is DeliveryState.DISABLED
    assert len(backend.requests) == 1


def test_plyer_never_delivers_an_audible_toast_when_silence_was_requested(hostile_event):
    from types import SimpleNamespace

    from gucmail.notification_providers.windows import (
        ToastSound,
        discover_windows_toast_backend,
    )

    calls = []
    backend, status = discover_windows_toast_backend(
        platform_name="win32",
        notification_api=SimpleNamespace(notify=lambda **kwargs: calls.append(kwargs)),
    )
    provider = WindowsToastProvider(
        backend, WindowsToastSettings(sound=ToastSound.SILENT), backend_status=status
    )
    result = provider.notify(hostile_event)
    assert result.state is DeliveryState.SKIPPED and result.attempts == 0
    assert not calls and not status.click_to_open
    assert "silent" in result.detail.casefold()
    assert WindowsToastProvider(backend).notify(hostile_event).state is DeliveryState.DELIVERED
    assert len(calls) == 1


def test_retry_schedule_honors_backoff_jitter_retry_after_and_limits() -> None:
    policy = RetryPolicy(
        maximum_attempts=3,
        base_delay=timedelta(seconds=10),
        maximum_delay=timedelta(minutes=2),
        jitter_ratio=0.2,
    )
    first = schedule_retry(
        policy,
        RetryContext(1, NOW, NOW, status_code=503),
        random_fraction=0,
    )
    assert first.disposition is RetryDisposition.RETRY
    assert first.delay == timedelta(seconds=8)
    server = schedule_retry(
        policy,
        RetryContext(2, NOW, NOW, status_code=429, retry_after="45"),
    )
    assert server.delay == timedelta(seconds=45)
    # A provider's Retry-After is a minimum and must not be shortened to the
    # client's ordinary exponential-backoff ceiling.
    long_server = schedule_retry(
        policy,
        RetryContext(2, NOW, NOW, status_code=429, retry_after="600"),
    )
    assert long_server.delay == timedelta(minutes=10)
    malformed_server = schedule_retry(
        policy,
        RetryContext(2, NOW, NOW, status_code=429, retry_after="inf"),
        random_fraction=0.5,
    )
    assert malformed_server.delay == timedelta(seconds=20)
    exhausted = schedule_retry(policy, RetryContext(3, NOW, NOW, status_code=503))
    assert exhausted.disposition is RetryDisposition.DEAD_LETTER
    permanent = schedule_retry(policy, RetryContext(1, NOW, NOW, status_code=400))
    assert permanent.disposition is RetryDisposition.DEAD_LETTER


def test_circuit_breaker_opens_recovers_half_open_and_closes() -> None:
    policy = CircuitBreakerPolicy(2, timedelta(minutes=5))
    state = record_circuit_result(CircuitState(), policy, NOW, succeeded=False)
    assert state.phase is CircuitPhase.CLOSED
    state = record_circuit_result(state, policy, NOW, succeeded=False)
    assert state.phase is CircuitPhase.OPEN
    blocked = circuit_decision(state, policy, NOW + timedelta(minutes=4))
    assert blocked.allowed is False
    probe = circuit_decision(state, policy, NOW + timedelta(minutes=5))
    assert probe.allowed is True
    assert probe.state.phase is CircuitPhase.HALF_OPEN
    assert record_circuit_result(probe.state, policy, NOW, succeeded=True) == CircuitState()


def test_provider_failure_isolation_and_retry_planning(hostile_event: NotificationEvent) -> None:
    class Provider:
        def __init__(self, name: str, outcome: str) -> None:
            self.name = name
            self.outcome = outcome
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            if self.outcome == "raise":
                raise TimeoutError("contains private@example.test")
            state = DeliveryState.DELIVERED if self.outcome == "ok" else DeliveryState.FAILED
            return DeliveryResult(
                event.event_id,
                self.name,
                state,
                status_code=None if state is DeliveryState.DELIVERED else 500,
            )

    windows = Provider("windows", "ok")
    discord = Provider("discord", "fail")
    generic = Provider("generic", "raise")
    outcomes = ProviderIsolationDispatcher([windows, discord, generic]).dispatch(
        hostile_event,
        now=NOW,
    )
    assert [item.result.state for item in outcomes] == [
        DeliveryState.DELIVERED,
        DeliveryState.FAILED,
        DeliveryState.FAILED,
    ]
    assert outcomes[0].retry.disposition is RetryDisposition.COMPLETE
    assert outcomes[1].retry.disposition is RetryDisposition.RETRY
    assert outcomes[2].retry.disposition is RetryDisposition.RETRY
    assert "private@example.test" not in str(outcomes)
    assert [windows.calls, discord.calls, generic.calls] == [1, 1, 1]


def test_provider_dispatch_dead_letters_after_bound_and_disambiguates_names(
    hostile_event: NotificationEvent,
) -> None:
    class Failure:
        name = "webhook"

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(event.event_id, self.name, DeliveryState.FAILED, status_code=503)

    dispatcher = ProviderIsolationDispatcher(
        [Failure(), Failure()],
        retry_policy=RetryPolicy(maximum_attempts=2),
    )
    outcomes = dispatcher.dispatch(
        hostile_event,
        now=NOW,
        attempt_numbers={"webhook": 2, "webhook#2": 2},
        first_attempts={"webhook": NOW, "webhook#2": NOW},
    )
    assert [item.binding_id for item in outcomes] == ["webhook", "webhook#2"]
    assert all(item.retry.disposition is RetryDisposition.DEAD_LETTER for item in outcomes)
    assert all(item.dead_letter is not None for item in outcomes)


def test_open_provider_circuit_does_not_invoke_backend(hostile_event: NotificationEvent) -> None:
    class Provider:
        name = "discord"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    provider = Provider()
    opened = CircuitState(CircuitPhase.OPEN, 3, NOW)
    outcome = ProviderIsolationDispatcher([provider]).dispatch(
        hostile_event,
        now=NOW + timedelta(minutes=1),
        circuit_states={"discord": opened},
    )[0]
    assert provider.calls == 0
    assert outcome.retry.disposition is RetryDisposition.RETRY
    assert outcome.retry.next_attempt_at == NOW + timedelta(minutes=5)


def test_provider_isolation_survives_a_broken_provider_name(
    hostile_event: NotificationEvent,
) -> None:
    class BrokenName:
        @property
        def name(self) -> str:
            raise RuntimeError("secret-bearing plugin failure")

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(event.event_id, "wrong", DeliveryState.DELIVERED)

    class Working:
        name = "working"

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    outcomes = ProviderIsolationDispatcher([BrokenName(), Working()]).dispatch(
        hostile_event,
        now=NOW,
    )

    assert [item.binding_id for item in outcomes] == ["invalid-provider-1", "working"]
    assert [item.result.state for item in outcomes] == [
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERED,
    ]
    assert outcomes[0].result.provider == "invalid-provider-1"


def test_prepared_request_repr_hides_destination_headers_and_mail_content(
    hostile_event: NotificationEvent,
) -> None:
    request = TelegramAdapter(
        TelegramConfig(TELEGRAM_TOKEN, "123456", PayloadPolicy(privacy=PrivacyMode.FULL))
    ).prepare(hostile_event)

    rendered = repr(request)
    assert TELEGRAM_TOKEN not in rendered
    assert "123456" not in rendered
    assert "@here" not in rendered
    assert "headers=" not in rendered
    assert "body=" not in rendered


def test_secret_bearing_provider_values_are_safe_for_structured_output(
    hostile_event: NotificationEvent,
) -> None:
    chat_id = "-100123456789"
    generic_url = "https://hooks.example.test/private/generic-token"
    authorization = "Bearer structured-output-secret"
    signing_secret = b"0123456789abcdef"
    values = (
        DiscordConfig(DISCORD_URL),
        TelegramConfig(TELEGRAM_TOKEN, chat_id),
        CompatibleWebhookConfig("https://hooks.example.test/private/compatible-token"),
        GenericWebhookConfig(
            "safe-output",
            generic_url,
            headers={"Authorization": authorization},
            query={"access_token": "query-secret"},
            hmac_secret=signing_secret,
        ),
        TelegramAdapter(TelegramConfig(TELEGRAM_TOKEN, chat_id)).prepare(hostile_event),
        TransportOutcome(
            False,
            503,
            detail=f"Delivery failed at {DISCORD_URL}",
            response_headers={"Location": generic_url, "Authorization": authorization},
        ),
        validate_destination(generic_url),
    )

    serialized = json.dumps(to_primitive(values), sort_keys=True)
    for private in (
        DISCORD_TOKEN,
        DISCORD_URL,
        TELEGRAM_TOKEN,
        chat_id,
        generic_url,
        "generic-token",
        "compatible-token",
        authorization,
        "structured-output-secret",
        "query-secret",
        signing_secret.decode(),
        "Important update",
    ):
        assert private not in serialized
    assert chat_id not in repr(TelegramConfig(TELEGRAM_TOKEN, chat_id))


def test_transport_detail_redacts_raw_secret_urls(hostile_event: NotificationEvent) -> None:
    class Transport:
        def deliver(self, _request):
            return TransportOutcome(
                False,
                status_code=503,
                detail=f"Provider rejected {DISCORD_URL}",
            )

    result = PreparedRequestNotificationProvider(
        DiscordAdapter(DiscordConfig(DISCORD_URL)),
        Transport(),
    ).notify(hostile_event)

    assert result.state is DeliveryState.FAILED
    assert result.detail is not None
    assert DISCORD_URL not in result.detail
    assert DISCORD_TOKEN not in result.detail
    assert "<redacted-url>" in result.detail
