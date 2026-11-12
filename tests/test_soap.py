from __future__ import annotations

import pytest
from lxml import etree

from gucmail.errors import UnsupportedFeatureError
from gucmail.ews.capabilities import CapabilityRegistry
from gucmail.ews.soap import (
    MESSAGES_NS,
    CopyFolderRequest,
    TypedSoapTransport,
    build_copy_folder,
    load_raw_request,
    message_element,
    parse_soap_response,
    preview_envelope,
)
from gucmail.models import (
    AccountAccess,
    CapabilityStatus,
    ImplementationPath,
    ImplementationStatus,
    SafetyClass,
    ServerSupport,
    VerificationLevel,
)


def test_copy_folder_serialization_order_and_namespaces() -> None:
    payload = build_copy_folder(CopyFolderRequest("dest", (("source", "change"),)))
    assert etree.QName(payload).localname == "CopyFolder"
    assert [etree.QName(child).localname for child in payload] == ["ToFolderId", "FolderIds"]
    envelope = preview_envelope(payload, api_version="Exchange2013")
    assert b"RequestServerVersion" in envelope
    assert b'Version="Exchange2013"' in envelope
    assert b"password" not in envelope.lower()


def test_parse_partial_soap_results() -> None:
    xml = f"""<?xml version='1.0'?>
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
      xmlns:m="{MESSAGES_NS}">
      <s:Body><m:CopyFolderResponse><m:ResponseMessages>
        <m:CopyFolderResponseMessage ResponseClass="Success">
          <m:ResponseCode>NoError</m:ResponseCode><m:Folders />
        </m:CopyFolderResponseMessage>
        <m:CopyFolderResponseMessage ResponseClass="Error">
          <m:MessageText>Denied</m:MessageText>
          <m:ResponseCode>ErrorAccessDenied</m:ResponseCode>
        </m:CopyFolderResponseMessage>
      </m:ResponseMessages></m:CopyFolderResponse></s:Body>
    </s:Envelope>""".encode()
    results = parse_soap_response(xml, "CopyFolder")
    assert [result.response_code for result in results] == ["NoError", "ErrorAccessDenied"]


def test_raw_request_loader_rejects_entities(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "request.xml"
    path.write_text(
        '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret">]>'
        f'<m:GetFolder xmlns:m="{MESSAGES_NS}">&e;</m:GetFolder>',
        encoding="utf-8",
    )
    try:
        load_raw_request(path)
    except UnsupportedFeatureError:
        pass
    else:
        raise AssertionError("entity-bearing request was accepted")


def test_typed_transport_rejects_operation_mismatch_before_dry_run() -> None:
    """A read-only registry name must never authorize a mutating XML payload."""

    class Account:
        version = type("Version", (), {"api_version": "Exchange2013"})()

    transport = TypedSoapTransport(Account())  # type: ignore[arg-type]
    with pytest.raises(UnsupportedFeatureError, match="does not match"):
        transport.execute("GetFolder", message_element("DeleteItem"), dry_run=True)


def test_typed_transport_rejects_registry_entries_without_a_typed_adapter() -> None:
    class Account:
        version = type("Version", (), {"api_version": "Exchange2013"})()

    transport = TypedSoapTransport(Account())  # type: ignore[arg-type]
    with pytest.raises(UnsupportedFeatureError, match="no registered typed SOAP adapter"):
        transport.execute("GetFolder", message_element("GetFolder"), dry_run=True)


@pytest.mark.parametrize(
    "copy_request",
    [
        CopyFolderRequest("", (("source", None),)),
        CopyFolderRequest("destination", ()),
        CopyFolderRequest("destination", tuple((str(index), None) for index in range(101))),
    ],
)
def test_copy_folder_builder_rejects_unbounded_or_incomplete_requests(
    copy_request: CopyFolderRequest,
) -> None:
    with pytest.raises(UnsupportedFeatureError):
        build_copy_folder(copy_request)


def test_capability_registry_axes_remain_not_probed() -> None:
    registry = CapabilityRegistry()
    send = registry.get("SendItem")
    quota = registry.get("MailboxQuota")
    discovery = registry.get("GetSearchableMailboxes")
    assert send.safety is SafetyClass.MUTATING
    assert send.server_support is ServerSupport.NOT_PROBED
    assert quota.implementation_status is ImplementationStatus.PARTIAL
    assert discovery.account_access is AccountAccess.ADMIN_ONLY
    assert len(registry.all()) >= 75


def test_monitoring_capabilities_do_not_claim_unimplemented_ews_push_or_sync() -> None:
    registry = CapabilityRegistry()

    for operation in (
        "SyncFolderHierarchy",
        "SubscribePull",
        "SubscribeStreaming",
        "SubscribePush",
        "GetEvents",
        "GetStreamingEvents",
        "Unsubscribe",
    ):
        capability = registry.get(operation)
        assert capability.implementation_path is ImplementationPath.REGISTRY_ONLY
        assert capability.implementation_status is ImplementationStatus.NOT_IMPLEMENTED
        assert capability.server_support is ServerSupport.NOT_PROBED

    assert "polling" in (registry.get("SyncFolderItems").fallback or "").casefold()
    assert registry.get("SyncFolderItems").implementation_status is ImplementationStatus.IMPLEMENTED
    assert "inbound" in (registry.get("SubscribePush").fallback or "").casefold()


def test_registry_does_not_claim_unexposed_advanced_mailbox_adapters() -> None:
    registry = CapabilityRegistry()

    for operation in (
        "GetFolderPermissions",
        "UpdateFolderPermissions",
        "GetUserConfiguration",
        "CreateUserConfiguration",
        "UpdateUserConfiguration",
        "DeleteUserConfiguration",
        "GetMailTips",
    ):
        capability = registry.get(operation)
        assert capability.implementation_path is ImplementationPath.REGISTRY_ONLY
        assert capability.implementation_status is ImplementationStatus.NOT_IMPLEMENTED

    recoverable = registry.get("RecoverableItems")
    assert recoverable.implementation_path is ImplementationPath.REGISTRY_ONLY
    assert recoverable.implementation_status is ImplementationStatus.NOT_IMPLEMENTED
    assert "deleted items" in (recoverable.fallback or "").casefold()


@pytest.mark.parametrize(
    "operation,implementation",
    [
        ("GetInboxRules", ImplementationStatus.IMPLEMENTED),
        ("UpdateInboxRules", ImplementationStatus.PARTIAL),
        ("GetUserOofSettings", ImplementationStatus.IMPLEMENTED),
        ("SetUserOofSettings", ImplementationStatus.IMPLEMENTED),
    ],
)
def test_exposed_mailbox_policies_keep_unprobed_server_and_account_axes(operation, implementation):
    capability = CapabilityRegistry().get(operation)
    assert capability.implementation_path is ImplementationPath.EXCHANGELIB
    assert capability.implementation_status is implementation
    assert capability.server_support is ServerSupport.NOT_PROBED
    assert capability.account_access is AccountAccess.NOT_PROBED
    assert capability.verification is VerificationLevel.FIXTURE_TESTED


def test_detection_cannot_promote_an_unimplemented_or_partial_adapter_to_live_verified() -> None:
    registry = CapabilityRegistry()

    unimplemented = registry.with_detection(
        "SyncFolderHierarchy",
        server_support=ServerSupport.VERSION_ELIGIBLE,
        account_access=AccountAccess.ALLOWED,
        verification=VerificationLevel.LIVE_READ,
    )
    partial = registry.with_detection(
        "MailboxQuota",
        server_support=ServerSupport.VERSION_ELIGIBLE,
        account_access=AccountAccess.ALLOWED,
        verification=VerificationLevel.LIVE_READ,
    )
    implemented = registry.with_detection(
        "SendItem",
        server_support=ServerSupport.VERSION_ELIGIBLE,
        account_access=AccountAccess.ALLOWED,
        verification=VerificationLevel.LIVE_MUTATION,
    )

    assert unimplemented.status is not CapabilityStatus.LIVE_VERIFIED
    assert partial.status is CapabilityStatus.PARTIAL
    assert implemented.status is CapabilityStatus.LIVE_VERIFIED
