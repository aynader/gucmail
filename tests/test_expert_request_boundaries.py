"""Encoded hostile XML and unavailable expert controls fail before execution."""

from pathlib import Path

import pytest

from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.ews.soap import MESSAGES_NS, SOAP_NS, load_raw_request, parse_soap_response
from gucmail.settings import SettingsService
from gucmail.tui.area_backends import SettingsAreaBackend
from test_settings_secondpass import _context


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_encoded_dtd_is_rejected_before_raw_operation_is_returned(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "request.xml"
    # No entity expansion is needed to reproduce the forbidden DTD being accepted.
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        "<!DOCTYPE m:GetFolder [<!ELEMENT m:GetFolder ANY>]>"
        f'<m:GetFolder xmlns:m="{MESSAGES_NS}" />'
    )
    path.write_bytes(xml.encode(encoding))
    with pytest.raises(UnsupportedFeatureError, match="DTD"):
        load_raw_request(path)


def test_unconnected_raw_execution_control_is_explained_and_cannot_be_enabled(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    try:
        service = SettingsService(context.config_store)
        value = service.value("advanced.raw_soap_enabled")
        view = SettingsAreaBackend(service)._setting(value)
        assert not view.editable
        assert "not implemented" in view.description
        before = context.paths.config_file.read_bytes()
        with pytest.raises(ConfigurationError, match="not implemented"):
            service.set("advanced.raw_soap_enabled", True)
        assert context.paths.config_file.read_bytes() == before
    finally:
        context.close()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be"])
def test_plain_encoded_operation_remains_available_without_dtd(
    tmp_path: Path, encoding: str
) -> None:
    path = tmp_path / "request.xml"
    declared = "UTF-16" if encoding.startswith("utf-16") else "UTF-8"
    path.write_bytes(
        (
            f'<?xml version="1.0" encoding="{declared}"?><m:GetFolder xmlns:m="{MESSAGES_NS}" />'
        ).encode(encoding)
    )
    root = load_raw_request(path, expected_operation="GetFolder")
    assert root.tag == f"{{{MESSAGES_NS}}}GetFolder"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be"])
def test_encoded_response_dtd_is_rejected_before_results_are_returned(encoding: str) -> None:
    declared = "UTF-16" if encoding.startswith("utf-16") else "UTF-8"
    data = (
        f'<?xml version="1.0" encoding="{declared}"?>'
        "<!DOCTYPE s:Envelope [<!ELEMENT s:Envelope ANY>]>"
        f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:m="{MESSAGES_NS}">'
        "<s:Body><m:GetFolderResponse><m:ResponseMessages>"
        '<m:GetFolderResponseMessage ResponseClass="Success">'
        "<m:ResponseCode>NoError</m:ResponseCode>"
        "</m:GetFolderResponseMessage></m:ResponseMessages>"
        "</m:GetFolderResponse></s:Body></s:Envelope>"
    ).encode(encoding)
    with pytest.raises(UnsupportedFeatureError, match="DTD"):
        parse_soap_response(data, "GetFolder")


@pytest.mark.parametrize("kind", ["request", "response"])
def test_malformed_xml_diagnostic_does_not_copy_untrusted_parser_context(
    tmp_path: Path, kind: str
) -> None:
    marker = "SyntheticPrivateMarker"
    data = f'<m:{marker} xmlns:m="{MESSAGES_NS}"><x></m:{marker}>'.encode()
    with pytest.raises(UnsupportedFeatureError) as caught:
        if kind == "request":
            path = tmp_path / "malformed.xml"
            path.write_bytes(data)
            load_raw_request(path)
        else:
            parse_soap_response(data, "GetFolder")
    assert marker not in str(caught.value)


def test_envelope_with_only_a_comment_cannot_return_a_raw_operation(tmp_path: Path) -> None:
    path = tmp_path / "comment.xml"
    path.write_text(f'<s:Envelope xmlns:s="{SOAP_NS}"><s:Body><!--comment--></s:Body></s:Envelope>')
    with pytest.raises(UnsupportedFeatureError, match="operation element"):
        load_raw_request(path)
