from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest

from gucmail.errors import ConfigurationError, UnsupportedFeatureError
from gucmail.ews.soap import MAX_RAW_SOAP_BYTES, load_raw_request
from gucmail.formatting import format_timestamp
from gucmail.models import MessageDetail, MessageSummary
from gucmail.settings import UNAVAILABLE_SETTINGS, SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.area_backends import SettingsAreaBackend
from test_settings_secondpass import _context


def test_date_preference_formats_reader_and_details_without_changing_model(tmp_path):
    context = _context(tmp_path)
    context.config = replace(
        context.config, datetime_format="%d/%m/%Y %H:%M [red]", timezone="Asia/Tokyo"
    )
    app = GUCmailApp(context, offline=True)
    timestamp = datetime(2026, 9, 10, 22, 30, tzinfo=UTC)
    detail = MessageDetail(
        summary=MessageSummary(
            reference="synthetic",
            sender=None,
            subject="Date",
            received_at=timestamp,
            sent_at=timestamp,
        )
    )
    for rendered in (app._format_message(detail), app._format_message_details(detail)):
        assert "11/09/2026 07:30" in rendered
        assert r"\[red]" in rendered
    assert detail.summary.received_at == timestamp
    assert format_timestamp(None, pattern="%Y", timezone="UTC") == ""
    assert "\x1b" not in format_timestamp(timestamp, pattern="%Y\x1b[2J", timezone="UTC")


@pytest.mark.parametrize("setting_id", tuple(UNAVAILABLE_SETTINGS))
def test_inert_preferences_are_explained_and_cannot_promise_a_runtime_effect(tmp_path, setting_id):
    service = SettingsService(_context(tmp_path).config_store)
    setting = service.value(setting_id)
    view = SettingsAreaBackend(service)._setting(setting)
    assert not view.editable
    assert view.description == UNAVAILABLE_SETTINGS[setting_id]
    with pytest.raises(ConfigurationError, match=r"not implemented|No generic|Hooks are enabled"):
        service.set(setting_id, setting.effective_value)


def test_raw_soap_file_is_bounded_before_xml_parse():
    class RequestFile:
        def open(self, mode):
            assert mode == "rb"
            return BytesIO(b"x" * (MAX_RAW_SOAP_BYTES + 1))

    with pytest.raises(UnsupportedFeatureError, match="2 MiB"):
        load_raw_request(RequestFile())
