from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests
from qa.live_readonly_smoke import (
    MAX_LOGICAL_REQUESTS,
    ReadOnlyRequestGuard,
    _soap_operation,
    _test_folders,
    _validate_sync_request,
)

from gucmail.errors import ItemNotFoundError


def _soap(operation: str) -> bytes:
    return (
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        b'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">'
        b"<s:Body><m:" + operation.encode("ascii") + b"/></s:Body></s:Envelope>"
    )


def test_guard_parser_accepts_one_allowlisted_soap_operation() -> None:
    assert _soap_operation(_soap("FindItem")) == "FindItem"


@pytest.mark.parametrize(
    "payload",
    [
        b"<!DOCTYPE x [<!ENTITY y SYSTEM 'file:///nope'>]><x>&y;</x>",
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>',
        (
            b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            b'xmlns:m="urn:test"><s:Body><m:One/><m:Two/></s:Body></s:Envelope>'
        ),
    ],
)
def test_guard_parser_fails_closed_on_unsafe_or_ambiguous_xml(payload: bytes) -> None:
    with pytest.raises(RuntimeError):
        _soap_operation(payload)


def test_request_guard_blocks_mutating_soap_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transported: list[object] = []

    def transport(_session: object, request: object, **_kwargs: Any) -> object:
        transported.append(request)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests.sessions.Session, "send", transport)
    guard = ReadOnlyRequestGuard("https://mail.guc.edu.eg/EWS/Exchange.asmx")
    with guard:
        request = SimpleNamespace(body=_soap("DeleteItem"))
        with pytest.raises(RuntimeError, match="not permitted"):
            requests.sessions.Session.send(object(), request)

    assert transported == []
    assert guard.logical == []


def test_request_guard_enforces_endpoint_method_and_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transported: list[str] = []

    def transport(_adapter: object, request: object, **_kwargs: Any) -> object:
        transported.append(str(getattr(request, "url", "")))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)
    endpoint = "https://mail.guc.edu.eg/EWS/Exchange.asmx"
    guard = ReadOnlyRequestGuard(endpoint)
    with guard:
        for url, method in (
            (endpoint + "?token=secret", "POST"),
            ("https://example.test/EWS/Exchange.asmx", "POST"),
            (endpoint, "GET"),
        ):
            request = SimpleNamespace(url=url, method=method)
            with pytest.raises(RuntimeError):
                requests.adapters.HTTPAdapter.send(object(), request)

        response = requests.adapters.HTTPAdapter.send(
            object(), SimpleNamespace(url=endpoint, method="POST")
        )

    assert response.status_code == 200
    assert transported == [endpoint]
    assert guard.physical_by_stage == {"setup": 1}

    monkeypatch.setattr(
        requests.sessions.Session,
        "send",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200),
    )
    budget_guard = ReadOnlyRequestGuard(endpoint)
    budget_guard.logical.extend(("setup", "FindItem") for _ in range(MAX_LOGICAL_REQUESTS))
    with budget_guard, pytest.raises(RuntimeError, match="budget"):
        requests.sessions.Session.send(object(), SimpleNamespace(body=_soap("FindItem")))


def test_folder_probe_classifies_missing_archive_without_exposing_values() -> None:
    class Gateway:
        def list_folders(self, **_kwargs: Any) -> tuple[SimpleNamespace, ...]:
            return (SimpleNamespace(reference="folder-local-ref"),)

        def get_folder(self, reference: str) -> object:
            assert reference == "archive"
            raise ItemNotFoundError("synthetic missing archive")

    result = _test_folders(Gateway())  # type: ignore[arg-type]

    assert result == (
        "bounded folder page returned 1 item(s); archive unavailable (cleanly classified)"
    )
    assert "folder-local-ref" not in result


@pytest.mark.parametrize(
    "size,field", [("4", "item:Subject"), ("3", "item:Body"), ("3", "item:Attachments")]
)
def test_native_sync_smoke_guard_rejects_unbounded_or_content_requests(size, field):
    body = (
        '<m:SyncFolderItems xmlns:m="urn:m" xmlns:t="urn:t">'
        "<m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>"
        f'<t:AdditionalProperties><t:FieldURI FieldURI="{field}"/></t:AdditionalProperties>'
        f"</m:ItemShape><m:MaxChangesReturned>{size}</m:MaxChangesReturned></m:SyncFolderItems>"
    ).encode()
    with pytest.raises(RuntimeError):
        _validate_sync_request(body)


def test_native_sync_smoke_guard_stays_separate_from_default_probe():
    normal = ReadOnlyRequestGuard("https://mail.guc.edu.eg/EWS/Exchange.asmx")
    sync = ReadOnlyRequestGuard("https://mail.guc.edu.eg/EWS/Exchange.asmx", native_sync=True)
    assert "SyncFolderItems" not in normal.allowed_operations
    assert sync.allowed_operations == {"ConvertId", "GetFolder", "SyncFolderItems"}
