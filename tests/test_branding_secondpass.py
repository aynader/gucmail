from __future__ import annotations

import hashlib
from pathlib import Path

from gucmail.tui.branding import (
    GUC_LOGO_SOURCE_SHA256,
    BrandColorBehavior,
    BrandingCatalog,
    BrandingPreferences,
    BrandSize,
    render_branding,
)
from gucmail.tui.themes import ColorCapability, ThemeRegistry


def test_repository_logo_has_a_direct_image_sample_with_verified_provenance() -> None:
    source = Path(__file__).parent / "fixtures" / "guc-logo-source.png"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == GUC_LOGO_SOURCE_SHA256

    preset = BrandingCatalog().get("builtin.guc-logo-image-sampled")
    assert preset.category == "logo"
    assert "alpha-mask sample" in preset.provenance
    assert GUC_LOGO_SOURCE_SHA256 in preset.provenance
    assert len(preset.full_lines) == 8
    assert len(preset.compact_lines) == 5


def test_auto_branding_uses_full_compact_and_minimal_terminal_variants() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    preferences = BrandingPreferences(preset_id="builtin.guc-logo-image-sampled")

    wide = render_branding(preferences, theme, terminal_width=118, max_height=8)
    narrow = render_branding(preferences, theme, terminal_width=77, max_height=6)
    tiny = render_branding(preferences, theme, terminal_width=32, max_height=6)

    assert wide.variant is BrandSize.FULL
    assert wide.height == 8
    assert narrow.variant is BrandSize.COMPACT
    assert narrow.height == 5
    assert tiny.variant is BrandSize.MINIMAL
    assert tiny.text == "GUC"


def test_theme_high_contrast_monochrome_and_hidden_rendering_are_legible() -> None:
    registry = ThemeRegistry()
    theme = registry.get("builtin.high-contrast-dark")
    preferences = BrandingPreferences(preset_id="builtin.guc-mail-standard")

    themed = render_branding(preferences, theme, terminal_width=118, max_height=8)
    themed_colors = {span.foreground for line in themed.lines for span in line.spans}
    assert themed_colors == {
        theme.palette.logo_primary,
        theme.palette.logo_secondary,
        theme.palette.logo_accent,
    }

    monochrome = render_branding(
        preferences,
        theme,
        terminal_width=118,
        max_height=8,
        capability=ColorCapability.MONOCHROME,
    )
    assert monochrome.text == themed.text
    assert all(
        span.foreground is None and span.background is None
        for line in monochrome.lines
        for span in line.spans
    )

    hidden = render_branding(
        BrandingPreferences(visible=False),
        theme,
        terminal_width=118,
        max_height=8,
    )
    assert hidden.lines == ()
    assert hidden.text == ""


def test_explicit_size_and_generated_image_resize_remain_user_controlled() -> None:
    theme = ThemeRegistry().get("builtin.default-dark")
    preferences = BrandingPreferences(
        preset_id="builtin.guc-logo-image-sampled",
        size=BrandSize.FULL,
        color_behavior=BrandColorBehavior.THEME,
        target_width=17,
        target_height=4,
    )

    rendered = render_branding(preferences, theme, terminal_width=40, max_height=6)

    assert rendered.variant is BrandSize.FULL
    assert rendered.width <= 17
    assert rendered.height == 4


def test_offline_figlet_catalog_is_broad_real_and_lazily_rendered() -> None:
    catalog = BrandingCatalog()
    font_presets = [preset for preset in catalog.presets if preset.font_backend == "pyfiglet"]

    assert len(font_presets) >= 570
    assert catalog.get("builtin.figlet-standard").category == "featured-figlet"
    assert catalog.get("builtin.figlet-ansi_shadow").category == "ansi-figlet"
    assert catalog.get("builtin.figlet-ascii12").category == "toilet-font"

    theme = ThemeRegistry().get("builtin.default-dark")
    standard = render_branding(
        BrandingPreferences(preset_id="builtin.figlet-standard", size=BrandSize.FULL),
        theme,
        terminal_width=100,
        max_height=10,
        catalog=catalog,
    )
    slant = render_branding(
        BrandingPreferences(preset_id="builtin.figlet-slant", size=BrandSize.FULL),
        theme,
        terminal_width=100,
        max_height=10,
        catalog=catalog,
    )

    assert standard.font_backend == "pyfiglet"
    assert standard.font_name == "standard"
    assert "GUC Mail" not in standard.text
    assert standard.text != slant.text
    assert standard.width <= 100
    assert standard.height <= 10


def test_requested_unavailable_font_is_visible_with_explicit_fallback() -> None:
    catalog = BrandingCatalog()
    fallback = catalog.get("builtin.figlet-fallback-babyfacelame")
    theme = ThemeRegistry().get("builtin.default-dark")

    rendered = render_branding(
        BrandingPreferences(preset_id=fallback.id, size=BrandSize.FULL),
        theme,
        terminal_width=80,
        max_height=8,
        catalog=catalog,
    )

    assert "Babyface Lame" in fallback.name
    assert fallback.category == "featured-figlet"
    assert rendered.fallback_used is True
    assert rendered.fallback_reason is not None
    assert "Standard is used explicitly" in rendered.fallback_reason
    assert rendered.text


def test_all_catalog_terminal_color_adaptations_remain_valid() -> None:
    from gucmail.tui.themes import adapt_theme, require_valid_theme, to_textual_theme

    for theme in ThemeRegistry().themes:
        for capability in ColorCapability:
            adapted = adapt_theme(theme, capability)
            require_valid_theme(adapted)
            to_textual_theme(adapted)


def test_all_branding_presets_fit_bounded_terminal_variants() -> None:
    catalog = BrandingCatalog()
    theme = ThemeRegistry().get("builtin.default-dark")
    for preset in catalog.presets:
        for width, height in ((32, 3), (79, 6), (120, 10)):
            result = render_branding(
                BrandingPreferences(preset_id=preset.id),
                theme,
                terminal_width=width,
                max_height=height,
                catalog=catalog,
            )
            assert result.width <= width, preset.id
            assert result.height <= height, preset.id
            assert "\x1b" not in result.text, preset.id
