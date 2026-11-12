"""Named monochrome presets must carry one coherent hue across every semantic role."""

import colorsys

import pytest

from gucmail.tui.themes import (
    ColorCapability,
    ThemeMode,
    ThemeRegistry,
    adapt_theme,
    contrast_ratio,
    validate_theme,
)

CASES = [
    ("Monochrome Dark", None),
    ("Monochrome Light", None),
    ("Monochrome Gray", None),
    ("Terminal Monochrome", None),
    ("Newspaper", None),
    ("Typewriter", None),
    ("Photocopier", None),
    ("E-Ink", None),
    ("White Phosphor", None),
    ("Black and White", None),
    ("White and Black", None),
    ("Monochrome Blue", (190, 235)),
    ("Monochrome Green", (100, 150)),
    ("Monochrome Amber", (25, 60)),
    ("Monochrome Red", (345, 20)),
    ("Monochrome Purple", (250, 300)),
    ("Monochrome Cyan", (165, 200)),
    ("Green Screen", (100, 150)),
    ("Amber Screen", (25, 60)),
    ("Blue Phosphor", (190, 235)),
]


@pytest.mark.parametrize("name,hue_range", CASES)
def test_named_monochrome_palette_has_no_unrelated_status_calendar_or_logo_hues(
    name: str, hue_range: tuple[int, int] | None
) -> None:
    theme = next(theme for theme in ThemeRegistry().builtins if theme.name == name)
    colored_roles = []
    for field, color in theme.palette.colors():
        channels = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
        if hue_range is None:
            assert len(set(channels)) == 1, (name, field, color)
        elif max(channels) - min(channels) >= 6:
            hue = colorsys.rgb_to_hls(*(value / 255 for value in channels))[0] * 360
            start, end = hue_range
            in_range = start <= hue <= end if start <= end else hue >= start or hue <= end
            assert in_range, (name, field, color, hue)
            colored_roles.append(field)
    if hue_range is not None:
        assert len(colored_roles) >= 12, "A named tinted preset must have visible tint"
    assert len(set(theme.palette.calendar_event_colors)) >= 4
    assert all(
        contrast_ratio(color, theme.palette.app_background) >= 3
        for color in theme.palette.calendar_event_colors
    )
    if name in {
        "Green Screen",
        "Amber Screen",
        "White Phosphor",
        "Blue Phosphor",
        "Black and White",
    }:
        assert theme.mode is ThemeMode.DARK
    if name == "White and Black":
        assert theme.mode is ThemeMode.LIGHT
    assert not validate_theme(theme)


@pytest.mark.parametrize("theme_id", ["builtin.default-light", "builtin.monochrome-light"])
def test_ansi16_muted_text_remains_readable_and_neutral(theme_id: str) -> None:
    original = ThemeRegistry().get(theme_id)
    projected = adapt_theme(original, ColorCapability.COLORS_16)
    assert contrast_ratio(projected.palette.muted_text, projected.palette.app_background) >= 4.5
    muted = projected.palette.muted_text
    assert muted[1:3] == muted[3:5] == muted[5:7]
    assert not validate_theme(projected)
