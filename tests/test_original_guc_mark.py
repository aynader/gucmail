from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gucmail.errors import ConfigurationError
from gucmail.settings import SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.branding import (
    BrandAlignment,
    BrandColorBehavior,
    BrandingCatalog,
    BrandingPreferences,
    BrandSize,
    render_branding,
)
from gucmail.tui.themes import ColorCapability, ThemeRegistry
from test_tui import context_for


def test_source_mark_is_colorful_and_preserves_prior_presets() -> None:
    catalog = BrandingCatalog()
    assert catalog.get("builtin.guc-original-mark").category == "logo"
    assert catalog.get("builtin.guc-mail-minimal").name == "GUC Mail Minimal"
    theme = ThemeRegistry().get("builtin.default-dark")
    result = render_branding(
        BrandingPreferences(preset_id="builtin.guc-original-mark"),
        theme,
        terminal_width=80,
        max_height=6,
    )
    assert result.variant is BrandSize.FULL
    assert result.height == 6
    assert "GUC MAIL" in result.text
    assert {"#ed1c24", "#ffcb08"} <= {
        color
        for line in result.lines
        for span in line.spans
        for color in (span.foreground, span.background)
        if color is not None
    }


def test_mark_uses_compact_or_plain_fallback_when_space_or_color_is_limited() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    base = BrandingPreferences(preset_id="builtin.guc-original-mark")
    compact = render_branding(base, theme, terminal_width=30, max_height=4)
    assert compact.variant is BrandSize.COMPACT
    assert compact.height == 4
    assert compact.width <= 30
    narrow = render_branding(base, theme, terminal_width=12, max_height=4)
    assert narrow.variant is BrandSize.MINIMAL
    assert narrow.text == "GUC Mail CLI"
    assert narrow.fallback_used
    plain = render_branding(
        BrandingPreferences(
            preset_id="builtin.guc-original-mark",
            color_behavior=BrandColorBehavior.MONOCHROME,
            alignment=BrandAlignment.CENTER,
        ),
        theme,
        terminal_width=30,
        max_height=8,
        capability=ColorCapability.MONOCHROME,
    )
    assert plain.text.strip() == "GUC Mail CLI"
    assert all(span.foreground is None for line in plain.lines for span in line.spans)
    ascii_only = render_branding(
        base, theme, terminal_width=30, max_height=8, unicode_supported=False
    )
    assert ascii_only.text == "GUC Mail CLI"


def test_mark_selection_survives_settings_reload(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("appearance.branding", "builtin.guc-original-mark")
    reopened = GUCmailApp(replace(context, config=context.config_store.load()), offline=True)
    assert reopened.branding_preferences.preset_id == "builtin.guc-original-mark"


def test_bundled_branding_choices_save_and_unknown_presets_are_rejected(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    settings = SettingsService(context.config_store)
    settings.set("appearance.branding", "builtin.guc-logo-image-sampled")
    assert settings.value("appearance.branding").effective_value == "builtin.guc-logo-image-sampled"
    with pytest.raises(ConfigurationError):
        settings.set("appearance.branding", "builtin.does-not-exist")
    assert settings.value("appearance.branding").effective_value == "builtin.guc-logo-image-sampled"


def test_settings_can_be_imported_first_in_a_fresh_process() -> None:
    flags = ["-I"]
    environment = None
    code = "import gucmail.settings; print('ok')"
    if os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1":
        from qa.offline.children import guarded_environment

        flags = []
        environment = guarded_environment()
        environment["GUCMAIL_AUDIT_IMPORT_PROBE"] = "settings"
        code = """
import sys, importlib
assert 'gucmail.settings' not in sys.modules
import gucmail.settings
from qa.offline.guard import OfflineViolation
guard = sys._gucmail_offline_guard
assert guard.ready
for name in ('gucmail.credentials', 'gucmail.ews.adapter', 'gucmail.bootstrap',
             'gucmail.notification_providers', 'exchangelib', 'keyring'):
    with guard.expected_denial():
        try:
            importlib.import_module(name)
        except OfflineViolation:
            pass
        else:
            raise AssertionError('Provider boundary was not blocked')
assert all(event['expected'] for event in guard.events)
print('ok')
"""
    result = subprocess.run(  # noqa: S603 - fixed owned probe; no shell or user input.
        [sys.executable, *flags, "-c", code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
