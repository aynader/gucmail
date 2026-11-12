"""Run five bounded, non-mutating checks against the configured GUC mailbox.

This intentionally bypasses the normal app context so it creates no application directories,
logs, or SQLite references.  It never prints credentials, mailbox values, item identifiers,
subjects, addresses, or response bodies.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time
import warnings
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

import requests  # noqa: E402

from gucmail.config import AppConfig  # noqa: E402
from gucmail.constants import DEFAULT_EWS_ENDPOINT  # noqa: E402
from gucmail.credentials import CredentialData, parse_legacy_credentials  # noqa: E402
from gucmail.errors import (  # noqa: E402
    AuthenticationError,
    ItemNotFoundError,
    NetworkError,
    ThrottledError,
    UnsupportedFeatureError,
)
from gucmail.ews.adapter import EWSAdapter  # noqa: E402

ALLOWED_SOAP_OPERATIONS = frozenset(
    {
        "ConvertId",
        "FindFolder",
        "FindItem",
        "GetFolder",
        "GetItem",
    }
)
MAX_LOGICAL_REQUESTS = 20
MAX_PHYSICAL_REQUESTS = 40


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _soap_operation(body: object) -> str:
    if isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        payload = bytes(body)
    else:
        raise RuntimeError("A request with an uninspectable body was blocked.")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise RuntimeError("A request containing an XML declaration construct was blocked.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(payload, parser=parser)
    soap_body = next((node for node in root.iter() if _local_name(node.tag) == "Body"), None)
    if soap_body is None or len(soap_body) != 1:
        raise RuntimeError("A request without exactly one SOAP operation was blocked.")
    return _local_name(soap_body[0].tag)


class ReadOnlyRequestGuard:
    """Fail closed on endpoints, methods, SOAP operations, and request budgets."""

    def __init__(self, endpoint: str, *, native_sync: bool = False) -> None:
        self.allowed_operations = (
            frozenset({"ConvertId", "GetFolder", "SyncFolderItems"})
            if native_sync
            else ALLOWED_SOAP_OPERATIONS
        )
        parsed = urlsplit(endpoint)
        self.expected = (
            parsed.scheme.casefold(),
            (parsed.hostname or "").casefold(),
            parsed.port or 443,
            parsed.path.rstrip("/"),
        )
        self.stage = "setup"
        self.logical: list[tuple[str, str]] = []
        self.physical_by_stage: Counter[str] = Counter()
        self.statuses: Counter[int] = Counter()
        self.transport_errors = 0
        self._original_session_send: Callable[..., Any] | None = None
        self._original_adapter_send: Callable[..., Any] | None = None

    def __enter__(self) -> ReadOnlyRequestGuard:
        self._original_session_send = requests.sessions.Session.send
        self._original_adapter_send = requests.adapters.HTTPAdapter.send
        guard = self

        def session_send(session: Any, request: Any, **kwargs: Any) -> Any:
            operation = _soap_operation(request.body)
            if operation not in guard.allowed_operations:
                raise RuntimeError(f"SOAP operation {operation!r} is not permitted by this test.")
            if operation == "SyncFolderItems":
                _validate_sync_request(request.body)
            if len(guard.logical) >= MAX_LOGICAL_REQUESTS:
                raise RuntimeError("The logical SOAP request budget was exhausted.")
            if guard.logical:
                time.sleep(2.0)
            guard.logical.append((guard.stage, operation))
            assert guard._original_session_send is not None
            return guard._original_session_send(session, request, **kwargs)

        def adapter_send(adapter: Any, request: Any, **kwargs: Any) -> Any:
            parsed = urlsplit(str(request.url))
            destination = (
                parsed.scheme.casefold(),
                (parsed.hostname or "").casefold(),
                parsed.port or 443,
                parsed.path.rstrip("/"),
            )
            if destination != guard.expected or parsed.query or parsed.fragment:
                raise RuntimeError("A request to an unexpected destination was blocked.")
            if str(request.method).upper() != "POST":
                raise RuntimeError("A non-POST request was blocked.")
            if sum(guard.physical_by_stage.values()) >= MAX_PHYSICAL_REQUESTS:
                raise RuntimeError("The physical HTTP request budget was exhausted.")
            guard.physical_by_stage[guard.stage] += 1
            assert guard._original_adapter_send is not None
            try:
                response = guard._original_adapter_send(adapter, request, **kwargs)
            except BaseException:
                guard.transport_errors += 1
                raise
            guard.statuses[int(response.status_code)] += 1
            return response

        type.__setattr__(requests.sessions.Session, "send", session_send)
        type.__setattr__(requests.adapters.HTTPAdapter, "send", adapter_send)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        assert self._original_session_send is not None
        assert self._original_adapter_send is not None
        type.__setattr__(requests.sessions.Session, "send", self._original_session_send)
        type.__setattr__(requests.adapters.HTTPAdapter, "send", self._original_adapter_send)

    @contextlib.contextmanager
    def for_stage(self, stage: str) -> Iterator[None]:
        previous = self.stage
        self.stage = stage
        try:
            yield
        finally:
            self.stage = previous


def _test_status(gateway: EWSAdapter) -> str:
    result = gateway.status()
    if result.get("connected") is not True or not result.get("exchange_version"):
        raise AssertionError("The status response was incomplete.")
    return "authenticated; Exchange version and bounded Inbox metadata available"


def _test_folders(gateway: EWSAdapter) -> str:
    folders = gateway.list_folders(parent_reference=None, limit=5, offset=0)
    if len(folders) > 5 or any(not folder.reference for folder in folders):
        raise AssertionError("The bounded folder response was structurally invalid.")
    # The archive is a separate distinguished root on some Exchange deployments,
    # so listing the normal message root does not prove that it is available.
    # Resolving it is read-only and reveals no mailbox value in this report.
    try:
        gateway.get_folder("archive")
    except (ItemNotFoundError, UnsupportedFeatureError):
        archive = "unavailable (cleanly classified)"
    else:
        archive = "available"
    return f"bounded folder page returned {len(folders)} item(s); archive {archive}"


def _test_messages(gateway: EWSAdapter) -> str:
    messages = gateway.list_messages(
        folder="inbox",
        limit=3,
        offset=0,
        order_by="-datetime_received",
    )
    if len(messages) > 3 or any(not message.reference for message in messages):
        raise AssertionError("The bounded message-summary response was structurally invalid.")
    return f"bounded header-only message page returned {len(messages)} item(s)"


def _test_calendar_metadata(gateway: EWSAdapter) -> str:
    start = datetime.now(UTC)
    end = start + timedelta(days=14)
    events = gateway.list_events(
        start=start,
        end=end,
        calendar_reference=None,
        limit=3,
        metadata_only=True,
    )
    for event in events:
        if event.start is None or event.end is None:
            raise AssertionError("A calendar metadata result was incomplete.")
    return f"14-day metadata-only calendar window returned {len(events)} item(s)"


def _test_pim_metadata(gateway: EWSAdapter) -> str:
    contacts = gateway.list_contacts(
        folder_reference=None,
        limit=3,
        metadata_only=True,
    )
    tasks = gateway.list_tasks(
        folder_reference=None,
        limit=3,
        metadata_only=True,
    )
    return f"metadata-only PIM pages returned {len(contacts)} contact(s) and {len(tasks)} task(s)"


def _validate_sync_request(body: bytes) -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(body, parser=parser)
    bound = root.find(".//{*}MaxChangesReturned")
    shape = root.find(".//{*}BaseShape")
    if bound is None or bound.text != "3" or shape is None or shape.text != "IdOnly":
        raise RuntimeError("Sync smoke requests must be IdOnly with a three-change bound.")
    forbidden = {"item:Body", "item:MimeContent", "item:Attachments", "item:TextBody"}
    if any(node.get("FieldURI") in forbidden for node in root.iter()):
        raise RuntimeError("Sync smoke requests cannot fetch bodies or attachment payloads.")


def _test_sync(gateway: EWSAdapter, folder: str) -> str:
    result = gateway.sync_folder_items(folder, max_changes=3)
    if not result.checkpoint or not result.baseline or len(result.items) > 3:
        raise AssertionError("The bounded synchronization response was incomplete.")
    return (
        f"metadata-only native sync returned {len(result.items)} change(s); "
        f"continuation={result.more_available}; no event delivery"
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run five bounded read-only GUC Exchange checks from one reused session."
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=REPOSITORY_ROOT / "creds.env",
        help="Path to the quoted email/username/password assignment file.",
    )
    parser.add_argument("--timeout", type=int, default=20, choices=range(5, 61), metavar="5-60")
    parser.add_argument(
        "--native-sync-only",
        action="store_true",
        help="Instead check one three-change Inbox page and one Calendar page; no folder drain.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    logging.disable(logging.CRITICAL)
    # Library conversion warnings may contain server-supplied field values.
    # Validation below reports bounded outcomes without exposing that payload.
    warnings.filterwarnings("ignore", category=UserWarning, module=r"exchangelib\..*")
    parsed = parse_legacy_credentials(args.credentials.resolve())
    values = parsed.values
    credentials = CredentialData(
        email=values["email"],
        username=values.get("username") or values["email"],
        password=values["password"],
        source="legacy_file",
    )
    del parsed, values
    config = AppConfig(
        email=credentials.email,
        username=credentials.username,
        endpoint=DEFAULT_EWS_ENDPOINT,
        request_timeout_seconds=args.timeout,
    ).validate()
    gateway = EWSAdapter(config=config, credentials=credentials, state=None)
    tests: tuple[tuple[str, Callable[[EWSAdapter], str]], ...] = (
        ("status", _test_status),
        ("folders", _test_folders),
        ("messages", _test_messages),
        ("calendar", _test_calendar_metadata),
        ("contacts_tasks", _test_pim_metadata),
    )
    if args.native_sync_only:
        tests = (
            ("sync_mail", lambda gateway: _test_sync(gateway, "inbox")),
            ("sync_calendar", lambda gateway: _test_sync(gateway, "calendar")),
        )
    passed = 0
    stopped = False
    try:
        with ReadOnlyRequestGuard(config.endpoint, native_sync=args.native_sync_only) as guard:
            for name, check in tests:
                if stopped:
                    print(
                        f"SKIP {name}: stopped after an authentication, throttling, "
                        "or network error"
                    )
                    continue
                try:
                    with guard.for_stage(name):
                        detail = check(gateway)
                except (AuthenticationError, ThrottledError, NetworkError) as exc:
                    stopped = True
                    print(f"FAIL {name}: {type(exc).__name__}; no private error text printed")
                except BaseException as exc:
                    stopped = True
                    print(f"FAIL {name}: {type(exc).__name__}; no private error text printed")
                else:
                    passed += 1
                    print(f"PASS {name}: {detail}")
            operations = Counter(operation for _stage, operation in guard.logical)
            operation_summary = (
                ", ".join(f"{operation}={count}" for operation, count in sorted(operations.items()))
                or "none"
            )
            status_summary = (
                ", ".join(f"{status}={count}" for status, count in sorted(guard.statuses.items()))
                or "none"
            )
            print(f"SUMMARY tests={passed}/{len(tests)}")
            print(
                "SUMMARY requests: "
                f"logical_soap={len(guard.logical)}, "
                f"physical_http={sum(guard.physical_by_stage.values())}, "
                f"transport_errors={guard.transport_errors}"
            )
            print(f"SUMMARY read-only SOAP operations: {operation_summary}")
            print(f"SUMMARY HTTP statuses (NTLM challenges included): {status_summary}")
            print("SUMMARY mutating SOAP operations allowed=0; mailbox writes attempted=0")
    finally:
        gateway.close()
        del credentials
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
