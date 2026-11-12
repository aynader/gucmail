from __future__ import annotations

import hashlib
import hmac
import io
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Lock

import httpx
import pytest

import gucmail.services.notifications as notifications_module
from gucmail.errors import ConfigurationError
from gucmail.models import Address, NotificationEvent
from gucmail.secret_store import InMemorySecretBackend, SecretStore
from gucmail.services.monitoring import MailboxMonitor
from gucmail.services.monitoring_outbox import (
    InMemoryEventOutbox,
    OutboxState,
    StateStoreEventOutbox,
)
from gucmail.services.notifications import (
    CommandHookProvider,
    ConsoleNotificationProvider,
    DeliveryResult,
    DeliveryState,
    NDJSONNotificationProvider,
    NotificationDispatcher,
    ProviderBinding,
    notification_event_schema,
)
from gucmail.services.webhooks import WebhookConfig, WebhookProvider, WebhookRegistry
from gucmail.state import MIGRATIONS, StateStore


def _event() -> NotificationEvent:
    return NotificationEvent(
        event_id="evt-123",
        event_type="new_message",
        occurred_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC),
        account_id="student@example.test",
        folder="Inbox",
        message_reference="m-1",
        sender=Address("sender@example.test", "\x1b[31mSender"),
        subject="Hello student@example.test",
        preview="A short preview",
    )


def test_notification_event_schema_matches_the_stable_payload_contract() -> None:
    schema = notification_event_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "event_id",
        "event_type",
        "occurred_at",
        "account_id",
    }
    assert set(schema["properties"]) == notifications_module.EVENT_FIELDS
    assert schema["properties"]["occurred_at"]["format"] == "date-time"


def test_console_and_ndjson_are_sanitized_and_allowlisted() -> None:
    console = io.StringIO()
    ConsoleNotificationProvider(console).notify(_event())
    rendered = console.getvalue()
    assert "\x1b" not in rendered
    assert "<redacted>@example.test" in rendered

    stream = io.StringIO()
    provider = NDJSONNotificationProvider(
        stream,
        field_allowlist={"event_id", "subject"},
    )
    provider.notify(_event())
    payload = json.loads(stream.getvalue())
    assert payload == {
        "event_id": "evt-123",
        "subject": "Hello <redacted>@example.test",
    }


def test_dispatcher_persists_per_provider_deduplication(tmp_path) -> None:
    class CountingProvider:
        name = "counter"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    state = StateStore(tmp_path / "state.sqlite3")
    provider = CountingProvider()
    dispatcher = NotificationDispatcher([provider], state=state)

    assert dispatcher.dispatch(_event())[0].state is DeliveryState.DELIVERED
    duplicate = dispatcher.dispatch(_event())[0]
    assert duplicate.state is DeliveryState.SKIPPED
    assert duplicate.attempts == 0
    assert provider.calls == 1


def test_dispatcher_claim_is_atomic_across_overlapping_watchers(tmp_path) -> None:
    class BlockingProvider:
        name = "counter"

        def __init__(self) -> None:
            self.calls = 0
            self.lock = Lock()
            self.entered = Event()
            self.release = Event()

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            with self.lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    state_path = tmp_path / "state.sqlite3"
    first_state = StateStore(state_path)
    second_state = StateStore(state_path)
    first_state.initialize()
    second_state.initialize()
    provider = BlockingProvider()
    first = NotificationDispatcher([provider], state=first_state)
    second = NotificationDispatcher([provider], state=second_state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(first.dispatch, _event())
        assert provider.entered.wait(timeout=5)
        duplicate = second.dispatch(_event())[0]
        provider.release.set()
        delivered = pending.result(timeout=5)[0]

    assert delivered.state is DeliveryState.DELIVERED
    assert duplicate.state is DeliveryState.DEFERRED
    assert duplicate.detail == "Delivery already in progress."
    assert provider.calls == 1


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("first_result", [DeliveryState.FAILED, DeliveryState.DELIVERED])
def test_in_progress_delivery_remains_retryable_without_spending_budget(
    tmp_path, durable: bool, first_result: DeliveryState
) -> None:
    class BlockingProvider:
        name = "counter"

        def __init__(self) -> None:
            self.calls = 0
            self.entered = Event()
            self.release = Event()

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                assert self.release.wait(timeout=5)
                return DeliveryResult(event.event_id, self.name, first_result)
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    state = StateStore(tmp_path / "state.sqlite3")
    outbox = (
        StateStoreEventOutbox(state, maximum_delivery_cycles=1)
        if durable
        else InMemoryEventOutbox(maximum_delivery_cycles=1)
    )
    provider = BlockingProvider()
    first = NotificationDispatcher([provider], state=state)
    second = NotificationDispatcher([provider], state=StateStore(tmp_path / "state.sqlite3"))
    event = _event()
    now = datetime.now(UTC)
    assert outbox.enqueue(event, folder="Inbox", detected_at=now)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(first.dispatch, event)
        assert provider.entered.wait(timeout=5)
        deferred = second.dispatch(event)
        recorded = outbox.record(event.event_id, deferred, now=now)
        assert recorded.state is OutboxState.RETRY_SCHEDULED
        assert recorded.delivery_cycles == 0
        assert not outbox.due(folder=None, now=now, limit=1)
        provider.release.set()
        assert pending.result(timeout=5)[0].state is first_result

    retry_time = now + timedelta(seconds=31)
    assert len(outbox.due(folder=None, now=retry_time, limit=1)) == 1
    result = second.dispatch(event)
    assert result[0].state is (
        DeliveryState.DELIVERED if first_result is DeliveryState.FAILED else DeliveryState.SKIPPED
    )
    assert outbox.record(event.event_id, result, now=retry_time).state is OutboxState.DELIVERED
    assert provider.calls == (2 if first_result is DeliveryState.FAILED else 1)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("new_result", [DeliveryState.DELIVERED, DeliveryState.DEFERRED])
def test_expired_outbox_claim_cannot_overwrite_new_owner_result(
    tmp_path, durable: bool, new_result: DeliveryState
) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    outbox = StateStoreEventOutbox(state) if durable else InMemoryEventOutbox()
    event = _event()
    now = event.occurred_at
    assert outbox.enqueue(event, folder="Inbox", detected_at=now)
    assert outbox.claim_due(
        folder=None, now=now, limit=1, lease_owner="worker-a", lease_for=timedelta(seconds=1)
    )
    later = now + timedelta(seconds=6)
    assert outbox.claim_due(
        folder=None, now=later, limit=1, lease_owner="worker-b", lease_for=timedelta(seconds=10)
    )
    outcome = outbox.record(
        event.event_id,
        [DeliveryResult(event.event_id, "provider", new_result)],
        now=later,
        lease_owner="worker-b",
    )
    with pytest.raises(RuntimeError):
        outbox.record(
            event.event_id,
            [DeliveryResult(event.event_id, "provider", DeliveryState.FAILED)],
            now=later,
            lease_owner="worker-a",
        )
    if new_result is DeliveryState.DELIVERED:
        assert outcome.state is OutboxState.DELIVERED
        assert not outbox.due(folder=None, now=later + timedelta(days=1), limit=1)
        with pytest.raises(RuntimeError):
            outbox.record(event.event_id, (), now=later)
    else:
        assert outcome.state is OutboxState.RETRY_SCHEDULED
        pending = outbox.due(folder=None, now=later + timedelta(seconds=31), limit=1)
        assert len(pending) == 1
        assert pending[0].delivery_cycles == 0


@pytest.mark.parametrize("durable", [False, True])
def test_unleased_record_cannot_override_claimed_outbox_entry(tmp_path, durable: bool) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    outbox = StateStoreEventOutbox(state) if durable else InMemoryEventOutbox()
    event = _event()
    now = event.occurred_at
    assert outbox.enqueue(event, folder="Inbox", detected_at=now)
    assert outbox.claim_due(
        folder=None, now=now, limit=1, lease_owner="worker-a", lease_for=timedelta(seconds=1)
    )
    with pytest.raises(RuntimeError):
        outbox.record(event.event_id, (), now=now + timedelta(milliseconds=100))
    assert (
        outbox.record(
            event.event_id, (), now=now + timedelta(milliseconds=200), lease_owner="worker-a"
        ).state
        is OutboxState.DELIVERED
    )


def test_monitor_uses_a_fresh_owner_for_each_outbox_claim(tmp_path) -> None:
    class CapturingOutbox:
        def __init__(self) -> None:
            self.owners: list[str] = []

        def claim_due(self, *, lease_owner: str, **_kwargs: object) -> tuple[()]:
            self.owners.append(lease_owner)
            return ()

    outbox = CapturingOutbox()
    monitor = MailboxMonitor(
        object(),
        StateStore(tmp_path / "state.sqlite3"),
        account_id="synthetic",
        dispatcher=NotificationDispatcher([]),
        outbox=outbox,  # type: ignore[arg-type]
    )
    assert monitor._drain_outbox() == ()
    assert monitor._drain_outbox() == ()
    assert len(outbox.owners) == 2
    assert outbox.owners[0] != outbox.owners[1]


def test_notification_history_keeps_failure_then_success(tmp_path) -> None:
    class FlakyProvider:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            state = DeliveryState.FAILED if self.calls == 1 else DeliveryState.DELIVERED
            return DeliveryResult(
                event.event_id,
                self.name,
                state,
                "failure for private@example.test" if state is DeliveryState.FAILED else None,
                attempts=self.calls,
                status_code=503 if state is DeliveryState.FAILED else 202,
            )

    dispatcher = NotificationDispatcher(
        [FlakyProvider()], state=StateStore(tmp_path / "state.sqlite3")
    )

    assert dispatcher.dispatch(_event())[0].state is DeliveryState.FAILED
    assert dispatcher.dispatch(_event())[0].state is DeliveryState.DELIVERED
    history = dispatcher.history(limit=10)

    assert [row["state"] for row in history] == ["delivered", "failed"]
    assert [row["status_code"] for row in history] == [202, 503]
    assert history[0]["attempts"] == 2
    assert history[0]["event_id"] == "evt-123"
    assert "private@example.test" not in str(history)


def test_notification_history_retention_is_bounded(tmp_path, monkeypatch) -> None:
    class Provider:
        name = "bounded"

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    monkeypatch.setattr(notifications_module, "_HISTORY_RETENTION", 3)
    dispatcher = NotificationDispatcher([Provider()], state=StateStore(tmp_path / "state.sqlite3"))
    for number in range(4):
        dispatcher.dispatch(replace(_event(), event_id=f"evt-{number}"))

    history = dispatcher.history(limit=10)
    assert [row["event_id"] for row in history] == ["evt-3", "evt-2", "evt-1"]


def test_command_hook_is_disabled_and_never_uses_a_shell() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    disabled = CommandHookProvider(["helper", "--event"], runner=runner)
    assert disabled.notify(_event()).state is DeliveryState.DISABLED
    assert calls == []

    enabled = CommandHookProvider(["helper", "--event"], enabled=True, runner=runner)
    assert enabled.notify(_event()).state is DeliveryState.DELIVERED
    command, kwargs = calls[0]
    assert command == ["helper", "--event"]
    assert kwargs["shell"] is False
    assert json.loads(str(kwargs["input"]))["event_id"] == "evt-123"


def test_webhook_requires_https() -> None:
    with pytest.raises(ConfigurationError, match="HTTPS"):
        WebhookConfig(name="unsafe", url="http://example.test/hook")


def test_signed_webhook_retries_offline_and_records_bounded_history(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(202)

    state = StateStore(tmp_path / "state.sqlite3")
    webhook_secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    sleeps: list[float] = []
    secret = b"test-signing-secret"
    config = WebhookConfig(
        name="automation",
        url="https://hooks.example.test/mail",
        secret=secret,
        max_retries=2,
        field_allowlist=frozenset({"event_id", "subject"}),
        redact=False,
    )
    provider = WebhookProvider(
        config,
        state=state,
        secret_store=webhook_secrets,
        client=client,
        sleep=sleeps.append,
        clock=lambda: datetime(2026, 8, 5, 12, 30, tzinfo=UTC),
        random_source=lambda: 0.0,
    )

    result = provider.notify(_event())

    assert result.state is DeliveryState.DELIVERED
    assert result.attempts == 2
    assert result.status_code == 202
    assert len(requests) == 2
    assert sleeps == [0.0]
    request = requests[0]
    assert json.loads(request.content) == {
        "event_id": "evt-123",
        "subject": "Hello student@example.test",
    }
    timestamp = "1785933000"
    expected = (
        "sha256="
        + hmac.new(
            secret,
            timestamp.encode() + b"." + request.content,
            hashlib.sha256,
        ).hexdigest()
    )
    assert request.headers["Idempotency-Key"] == "evt-123"
    assert request.headers["X-GUCMail-Timestamp"] == timestamp
    assert request.headers["X-GUCMail-Signature"] == expected
    assert [row["status_code"] for row in provider.delivery_history()] == [202, 503]


def test_webhook_closes_responses_without_buffering_their_bodies() -> None:
    class UnreadableBody(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("The webhook response body must not be read")
            yield b""  # pragma: no cover

        def close(self) -> None:
            self.closed = True

    streams: list[UnreadableBody] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = UnreadableBody()
        streams.append(stream)
        return httpx.Response(
            503 if len(streams) == 1 else 202,
            headers={"Retry-After": "0"},
            stream=stream,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = WebhookProvider(
            WebhookConfig(name="bounded", url="https://hooks.example.test", max_retries=1),
            client=client,
            sleep=lambda _delay: None,
        )
        result = provider.notify(_event())

    assert result.state is DeliveryState.DELIVERED
    assert result.attempts == 2
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


def test_webhook_ignores_nonfinite_retry_after() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 202, headers={"Retry-After": "NaN"})

    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = WebhookProvider(
            WebhookConfig(name="retry", url="https://hooks.example.test", max_retries=1),
            client=client,
            sleep=delays.append,
            random_source=lambda: 0.0,
        )
        assert provider.notify(_event()).state is DeliveryState.DELIVERED
    assert delays == [0.5]


def test_webhook_does_not_follow_redirects() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://internal.test/secret"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    provider = WebhookProvider(
        WebhookConfig(name="redirect", url="https://hooks.example.test", max_retries=0),
        client=client,
    )
    result = provider.notify(_event())
    assert result.state is DeliveryState.FAILED
    assert result.status_code == 302
    assert calls == 1


def test_webhook_registry_retains_delivery_options_after_restart(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    secret_store = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(StateStore(state_path), secret_store=secret_store)
    configured = WebhookConfig(
        name="local",
        url="https://127.0.0.1/hook",
        allow_private_target=True,
        allow_integration_events=True,
        redact=False,
        connect_timeout_seconds=2,
        request_timeout_seconds=8,
        retry_base_seconds=3,
        retry_max_seconds=20,
        history_limit=42,
    )
    registry.add(configured)

    reopened = WebhookRegistry(StateStore(state_path), secret_store=secret_store).get("local")
    assert reopened is not None
    for option in (
        "allow_private_target",
        "allow_integration_events",
        "redact",
        "connect_timeout_seconds",
        "request_timeout_seconds",
        "retry_base_seconds",
        "retry_max_seconds",
        "history_limit",
    ):
        assert getattr(reopened, option) == getattr(configured, option)


def test_webhook_registry_rejects_malformed_boolean_options(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    secrets = SecretStore("webhooks", backend=InMemorySecretBackend())
    registry = WebhookRegistry(state, secret_store=secrets)
    registry.add(WebhookConfig(name="safe", url="https://hooks.example.test"))
    with state.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE webhooks SET delivery_options_json=? WHERE name=?",
            ('{"allow_private_target": "false"}', "safe"),
        )
    result = registry.load()
    assert not result.configurations
    assert len(result.issues) == 1


def test_webhook_schema_upgrade_preserves_legacy_rows_and_defaults(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 12):
            connection.executescript(MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "legacy"),
            )
        connection.execute(
            "INSERT INTO webhooks(name, url, created_at) VALUES (?, ?, ?)",
            ("legacy", "keyring://legacy-destination", "legacy"),
        )

    state = StateStore(state_path)
    registry = WebhookRegistry(state)
    loaded = registry.get("legacy")
    assert loaded is not None
    assert loaded.url == "keyring://legacy-destination"
    assert loaded.redact is True
    assert loaded.allow_private_target is False
    assert loaded.request_timeout_seconds == 15
    with state.connection() as connection:
        row = connection.execute(
            "SELECT delivery_options_json, created_at FROM webhooks WHERE name='legacy'"
        ).fetchone()
    assert tuple(row) == ("{}", "legacy")


def test_registry_never_persists_runtime_signing_secret(tmp_path) -> None:
    secret_store = SecretStore("webhooks", backend=InMemorySecretBackend())
    secret_store.put_and_verify("gucmail-webhook-secure", "runtime-only-secret")
    state = StateStore(tmp_path / "state.sqlite3")
    registry = WebhookRegistry(
        state,
        secret_store=secret_store,
    )
    stored = registry.add(
        WebhookConfig(
            name="secure",
            url="https://hooks.example.test",
            secret=b"runtime-only-secret",
            secret_keyring_name="gucmail-webhook-secure",
        )
    )
    assert stored.webhook_id is not None
    loaded = registry.get("SECURE")
    assert loaded is not None
    assert loaded.secret is None
    assert loaded.secret_keyring_name == "gucmail-webhook-secure"
    assert registry.set_enabled("secure", enabled=False).enabled is False
    assert registry.set_enabled("secure", enabled=True).enabled is True

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    with WebhookProvider(
        loaded,
        state=state,
        secret_store=secret_store,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ) as provider:
        assert provider.notify(_event()).state is DeliveryState.DELIVERED
    assert requests[0].headers["X-GUCMail-Signature"].startswith("sha256=")


def test_dispatcher_exposes_bounded_sanitized_history(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    dispatcher = NotificationDispatcher([ConsoleNotificationProvider(io.StringIO())], state=state)
    dispatcher.dispatch(_event())
    history = dispatcher.history(limit=1)
    assert len(history) == 1
    assert history[0]["provider"] == "console"
    assert history[0]["event_id"] == "evt-123"
    assert history[0]["attempts"] == 1
    with pytest.raises(ValueError, match="between 1 and 500"):
        dispatcher.history(limit=0)


def test_dispatcher_never_persists_raw_secret_urls_in_failure_detail(tmp_path) -> None:
    secret_url = "https://discord.com/api/webhooks/123456789/synthetic-private-token"

    class FailedProvider:
        name = "synthetic-webhook"

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            return DeliveryResult(
                event.event_id,
                self.name,
                DeliveryState.FAILED,
                f"Rejected by {secret_url}",
            )

    state = StateStore(tmp_path / "state.sqlite3")
    dispatcher = NotificationDispatcher([FailedProvider()], state=state)
    result = dispatcher.dispatch(_event())[0]

    assert secret_url not in (result.detail or "")
    with state.connection() as connection:
        stored = connection.execute(
            "SELECT detail FROM notification_history ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert secret_url not in stored
    assert "synthetic-private-token" not in stored
    assert secret_url not in json.dumps(dispatcher.history())


def test_malformed_provider_is_isolated_from_later_providers() -> None:
    class BrokenPolicy:
        def matches(self, _event: NotificationEvent) -> bool:
            raise ValueError("contains private@example.test")

    class BrokenProvider:
        @property
        def name(self) -> str:
            raise RuntimeError("invalid provider name")

        def notify(self, _event: NotificationEvent) -> DeliveryResult:
            raise AssertionError("policy failure must skip provider invocation")

    class WorkingProvider:
        name = "working"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    working = WorkingProvider()
    dispatcher = NotificationDispatcher(
        [
            ProviderBinding(BrokenProvider(), policy=BrokenPolicy()),  # type: ignore[arg-type]
            working,
        ],
        deduplicate=False,
    )

    results = dispatcher.dispatch(_event())

    assert [result.state for result in results] == [
        DeliveryState.FAILED,
        DeliveryState.DELIVERED,
    ]
    assert results[0].provider == "invalid-provider-1"
    assert "private@example.test" not in str(results)
    assert working.calls == 1


def test_notification_history_failure_does_not_block_provider_fanout(monkeypatch) -> None:
    class WorkingProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    first = WorkingProvider("first")
    second = WorkingProvider("second")
    dispatcher = NotificationDispatcher([first, second], deduplicate=False)

    def fail_record(*_args, **_kwargs) -> None:
        raise RuntimeError("synthetic local database failure")

    monkeypatch.setattr(dispatcher, "_record", fail_record)

    results = dispatcher.dispatch(_event())

    assert [result.state for result in results] == [
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERED,
    ]
    assert first.calls == second.calls == 1


def test_duplicate_provider_names_receive_distinct_deduplication_keys(tmp_path) -> None:
    class Provider:
        name = "duplicate"

        def __init__(self) -> None:
            self.calls = 0

        def notify(self, event: NotificationEvent) -> DeliveryResult:
            self.calls += 1
            return DeliveryResult(event.event_id, self.name, DeliveryState.DELIVERED)

    first = Provider()
    second = Provider()
    dispatcher = NotificationDispatcher(
        [first, second],
        state=StateStore(tmp_path / "state.sqlite3"),
    )

    results = dispatcher.dispatch(_event())

    assert [result.provider for result in results] == ["duplicate", "duplicate#2"]
    assert [result.state for result in results] == [
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERED,
    ]
    assert first.calls == second.calls == 1
