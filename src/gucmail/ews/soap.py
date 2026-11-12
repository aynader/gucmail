"""Narrow typed SOAP extension layer reusing exchangelib's authenticated protocol."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lxml import etree

from gucmail.errors import ConfirmationRequiredError, UnsupportedFeatureError
from gucmail.ews.capabilities import DEFAULT_REGISTRY, CapabilityRegistry
from gucmail.models import ImplementationPath, ImplementationStatus, SafetyClass

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
MESSAGES_NS = "http://schemas.microsoft.com/exchange/services/2006/messages"
TYPES_NS = "http://schemas.microsoft.com/exchange/services/2006/types"
NAMESPACES = {"s": SOAP_NS, "m": MESSAGES_NS, "t": TYPES_NS}
SAFE_OPERATION_NAME = re.compile(r"^[A-Z][A-Za-z0-9]{1,80}$")
MAX_RAW_SOAP_BYTES = 2 * 1024 * 1024
MAX_TYPED_BATCH_ITEMS = 100
MAX_EWS_ID_CHARS = 8_192


@dataclass(frozen=True, slots=True)
class SoapResult:
    operation: str
    response_class: str
    response_code: str
    message: str | None
    elements: tuple[etree._Element, ...]


@dataclass(frozen=True, slots=True)
class CopyFolderRequest:
    destination_folder_id: str
    folder_ids: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class MarkAllItemsReadRequest:
    folder_id: str
    change_key: str | None = None
    suppress_read_receipts: bool = True


class AccountLike(Protocol):
    protocol: Any
    version: Any
    primary_smtp_address: str


def message_element(operation: str) -> etree._Element:
    if not SAFE_OPERATION_NAME.fullmatch(operation):
        raise UnsupportedFeatureError("Invalid SOAP operation name.")
    return etree.Element(etree.QName(MESSAGES_NS, operation))


def _ews_identifier(value: str, *, label: str) -> str:
    """Validate one opaque Exchange identifier without logging its value."""

    if not isinstance(value, str):
        raise UnsupportedFeatureError(f"{label} must be text.")
    normalized = value.strip()
    if not normalized:
        raise UnsupportedFeatureError(f"{label} is required.")
    if len(normalized) > MAX_EWS_ID_CHARS or any(ord(character) < 32 for character in normalized):
        raise UnsupportedFeatureError(f"{label} is invalid or too long.")
    return normalized


def build_copy_folder(request: CopyFolderRequest) -> etree._Element:
    destination_id = _ews_identifier(
        request.destination_folder_id, label="Destination folder identifier"
    )
    if not request.folder_ids:
        raise UnsupportedFeatureError("CopyFolder requires at least one source folder.")
    if len(request.folder_ids) > MAX_TYPED_BATCH_ITEMS:
        raise UnsupportedFeatureError(
            f"CopyFolder is limited to {MAX_TYPED_BATCH_ITEMS} source folders per request."
        )
    root = message_element("CopyFolder")
    destination = etree.SubElement(root, etree.QName(MESSAGES_NS, "ToFolderId"))
    etree.SubElement(
        destination,
        etree.QName(TYPES_NS, "FolderId"),
        Id=destination_id,
    )
    folder_ids = etree.SubElement(root, etree.QName(MESSAGES_NS, "FolderIds"))
    for folder_id, change_key in request.folder_ids:
        attributes = {"Id": _ews_identifier(folder_id, label="Source folder identifier")}
        if change_key:
            attributes["ChangeKey"] = _ews_identifier(change_key, label="Folder change key")
        etree.SubElement(folder_ids, etree.QName(TYPES_NS, "FolderId"), **attributes)
    return root


def preview_envelope(payload: etree._Element, *, api_version: str = "Exchange2016") -> bytes:
    """Build a deterministic, credential-free request preview for documentation/debugging."""

    envelope = etree.Element(etree.QName(SOAP_NS, "Envelope"), nsmap=NAMESPACES)
    header = etree.SubElement(envelope, etree.QName(SOAP_NS, "Header"))
    etree.SubElement(
        header,
        etree.QName(TYPES_NS, "RequestServerVersion"),
        Version=api_version,
    )
    body = etree.SubElement(envelope, etree.QName(SOAP_NS, "Body"))
    body.append(payload)
    return bytes(
        etree.tostring(envelope, encoding="utf-8", xml_declaration=True, pretty_print=True)
    )


def parse_soap_response(data: bytes, operation: str) -> tuple[SoapResult, ...]:
    if len(data) > MAX_RAW_SOAP_BYTES:
        raise UnsupportedFeatureError("SOAP response exceeds the safe parsing limit.")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UnsupportedFeatureError("Malformed SOAP response.") from exc
    if root.getroottree().docinfo.doctype:
        raise UnsupportedFeatureError("DTD declarations are forbidden in SOAP responses.")
    faults = root.xpath("/s:Envelope/s:Body/s:Fault", namespaces=NAMESPACES)
    if faults:
        code = faults[0].findtext("faultcode") or "SOAPFault"
        message = faults[0].findtext("faultstring") or "Exchange returned a SOAP fault."
        return (SoapResult(operation, "Error", code, message, ()),)
    response_messages = root.xpath(
        "/s:Envelope/s:Body/*[local-name()=$response]/m:ResponseMessages/*",
        namespaces=NAMESPACES,
        response=f"{operation}Response",
    )
    if not response_messages:
        # Some EWS operations place one ResponseMessage directly below the response element.
        response_messages = root.xpath(
            "/s:Envelope/s:Body/*[local-name()=$response]/*"
            "[contains(local-name(), 'ResponseMessage')]",
            namespaces=NAMESPACES,
            response=f"{operation}Response",
        )
    results: list[SoapResult] = []
    for item in response_messages:
        response_class = item.get("ResponseClass", "Unknown")
        code = item.findtext(etree.QName(MESSAGES_NS, "ResponseCode")) or "Unknown"
        message = item.findtext(etree.QName(MESSAGES_NS, "MessageText"))
        elements = tuple(
            child
            for child in item
            if etree.QName(child).localname not in {"ResponseCode", "MessageText", "MessageXml"}
        )
        results.append(SoapResult(operation, response_class, code, message, elements))
    if not results:
        raise UnsupportedFeatureError(
            f"SOAP response did not contain the expected {operation} response messages."
        )
    return tuple(results)


class TypedSoapTransport:
    """Execute registered extensions using exchangelib's session, version, faults, and backoff."""

    def __init__(
        self,
        account: AccountLike,
        registry: CapabilityRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self.account = account
        self.registry = registry

    def execute(
        self,
        operation: str,
        payload: etree._Element,
        *,
        container_name: str | None = None,
        confirmed: bool = False,
        dry_run: bool = False,
    ) -> tuple[Any, ...] | bytes:
        capability = self.registry.get(operation)
        canonical_operation = capability.operation
        try:
            payload_name = etree.QName(payload).localname
            payload_namespace = etree.QName(payload).namespace
        except (TypeError, ValueError) as exc:
            raise UnsupportedFeatureError("Typed SOAP payload must be one XML element.") from exc
        if payload_namespace != MESSAGES_NS or payload_name != canonical_operation:
            raise UnsupportedFeatureError(
                "SOAP payload operation does not match the registered operation."
            )
        if container_name is not None and not SAFE_OPERATION_NAME.fullmatch(container_name):
            raise UnsupportedFeatureError("Invalid SOAP response container name.")
        if len(etree.tostring(payload, encoding="utf-8")) > MAX_RAW_SOAP_BYTES:
            raise UnsupportedFeatureError("Typed SOAP request exceeds the 2 MiB safety limit.")
        if capability.implementation_status is ImplementationStatus.NOT_IMPLEMENTED:
            raise UnsupportedFeatureError(
                capability.reason or f"{operation} is represented but not implemented."
            )
        if capability.implementation_path is not ImplementationPath.SOAP_TYPED:
            raise UnsupportedFeatureError(
                f"{canonical_operation} has no registered typed SOAP adapter."
            )
        if capability.safety is not SafetyClass.READ_ONLY and not confirmed and not dry_run:
            raise ConfirmationRequiredError(
                f"{operation} is {capability.safety.value}; confirmation is required."
            )
        if dry_run:
            api_version = getattr(getattr(self.account, "version", None), "api_version", None)
            return preview_envelope(payload, api_version=api_version or "Exchange2016")

        from exchangelib.services.common import EWSAccountService

        attributes: dict[str, Any] = {
            "SERVICE_NAME": canonical_operation,
            "element_container_name": container_name,
            "returns_elements": container_name is not None,
        }
        service_type = type(f"GUCmail{operation}Service", (EWSAccountService,), attributes)
        service = service_type(account=self.account)
        return tuple(service._get_elements(payload=payload))


def load_raw_request(path: Path, *, expected_operation: str | None = None) -> etree._Element:
    """Parse an expert request file without entities, DTDs, network access, or interpolation."""

    with path.open("rb") as handle:
        data = handle.read(MAX_RAW_SOAP_BYTES + 1)
    if len(data) > MAX_RAW_SOAP_BYTES:
        raise UnsupportedFeatureError("Raw SOAP request exceeds the 2 MiB safety limit.")
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise UnsupportedFeatureError("DTD and entity declarations are forbidden in raw SOAP.")
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UnsupportedFeatureError("Invalid XML request.") from exc
    # Inspect parsed document metadata as well as bytes: UTF-16/32 declarations
    # do not contain the ASCII byte sequence used by the early rejection above.
    if root.getroottree().docinfo.doctype:
        raise UnsupportedFeatureError("DTD and entity declarations are forbidden in raw SOAP.")
    if etree.QName(root).namespace == SOAP_NS and etree.QName(root).localname == "Envelope":
        body = root.find(f"{{{SOAP_NS}}}Body")
        if body is None or len(body) != 1:
            raise UnsupportedFeatureError("SOAP Envelope must contain exactly one operation.")
        root = body[0]
    if not isinstance(root.tag, str):
        raise UnsupportedFeatureError("Raw SOAP must contain one EWS operation element.")
    if etree.QName(root).namespace != MESSAGES_NS:
        raise UnsupportedFeatureError("Raw operation must use the EWS messages namespace.")
    operation = etree.QName(root).localname
    if not SAFE_OPERATION_NAME.fullmatch(operation):
        raise UnsupportedFeatureError("Invalid raw SOAP operation name.")
    if expected_operation and operation.casefold() != expected_operation.casefold():
        raise UnsupportedFeatureError(
            f"Request contains {operation}, not the expected {expected_operation}."
        )
    return root
