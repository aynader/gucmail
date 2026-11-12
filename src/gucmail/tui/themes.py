"""Semantic, accessible themes for the Textual interface.

The theme engine is intentionally independent from Textual.  It owns stable theme
identifiers, validation, terminal-colour adaptation, custom-theme interchange, and
preview state.  The TUI only has to translate semantic tokens to framework CSS.

Built-ins are generated from curated family recipes.  Generation is deterministic
and every definition records its recipe provenance; built-ins are not aliases.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import os
import random
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from textual.theme import Theme as TextualTheme

THEME_SCHEMA_VERSION: Final = 1
GENERATOR_VERSION: Final = "gucmail-harmonic-v1"
_HEX_RE: Final = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID_RE: Final = re.compile(r"^(?:builtin|custom)\.[a-z0-9][a-z0-9._-]{0,95}$")


class ThemeValidationError(ValueError):
    """Raised when a theme cannot be loaded safely."""


class ThemeMode(StrEnum):
    DARK = "dark"
    LIGHT = "light"


class ContrastClass(StrEnum):
    STANDARD = "standard"
    HIGH = "high"
    MAXIMUM = "maximum"
    REDUCED_GLARE = "reduced_glare"


class ColorCapability(StrEnum):
    TRUECOLOR = "truecolor"
    COLORS_256 = "256"
    COLORS_16 = "16"
    MONOCHROME = "monochrome"


class ThemePolicy(StrEnum):
    EXPLICIT = "explicit"
    SYSTEM = "system"
    TIME = "time"
    DARK = "dark"
    LIGHT = "light"
    RANDOM = "random"


class ValidationSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ThemeIssue:
    severity: ValidationSeverity
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class ThemePalette:
    """Complete semantic visual vocabulary used throughout the application."""

    app_background: str
    secondary_background: str
    sidebar_background: str
    message_list_background: str
    reading_pane_background: str
    elevated_surface: str
    command_palette_background: str
    modal_background: str
    primary_text: str
    secondary_text: str
    muted_text: str
    disabled_text: str
    primary_accent: str
    secondary_accent: str
    tertiary_accent: str
    border: str
    divider: str
    focus_ring: str
    hover: str
    active: str
    selection_background: str
    selection_foreground: str
    unread_message: str
    read_message: str
    flagged: str
    important: str
    link: str
    success: str
    information: str
    warning: str
    error: str
    quota_safe: str
    quota_warning: str
    quota_critical: str
    calendar_event_colors: tuple[str, ...]
    contact_highlight: str
    search_highlight: str
    command_result_highlight: str
    logo_primary: str
    logo_secondary: str
    logo_accent: str
    scrollbar_track: str
    scrollbar_thumb: str
    footer: str
    header: str

    def colors(self) -> Iterable[tuple[str, str]]:
        """Yield every named colour, flattening the calendar palette."""

        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, tuple):
                for index, color in enumerate(value):
                    yield f"{field.name}[{index}]", color
            else:
                yield field.name, value


@dataclass(frozen=True, slots=True)
class ThemeStyle:
    border_style: str = "solid"
    divider_style: str = "solid"
    selection_style: str = "filled"
    bold_text: bool = True
    row_density: str = "comfortable"
    banner_treatment: str = "flat"
    surface_elevation: str = "subtle"
    accent_intensity: str = "balanced"


@dataclass(frozen=True, slots=True)
class ThemeProvenance:
    source: str
    family: str
    recipe: str
    generator_version: str = GENERATOR_VERSION
    parent_theme_id: str | None = None


@dataclass(frozen=True, slots=True)
class ThemeDefinition:
    id: str
    name: str
    categories: tuple[str, ...]
    mode: ThemeMode
    contrast: ContrastClass
    palette: ThemePalette
    style: ThemeStyle
    provenance: ThemeProvenance
    search_tags: tuple[str, ...] = ()
    color_blind_friendly: bool = False
    schema_version: int = THEME_SCHEMA_VERSION

    @property
    def visual_signature(self) -> tuple[object, ...]:
        """A stable signature used to prove built-ins are not palette aliases."""

        return (
            *tuple(color for _, color in self.palette.colors()),
            *asdict(self.style).values(),
        )


@dataclass(frozen=True, slots=True)
class ThemeRequest:
    policy: ThemePolicy = ThemePolicy.EXPLICIT
    selected_theme_id: str = "builtin.default-dark"
    dark_theme_id: str = "builtin.default-dark"
    light_theme_id: str = "builtin.default-light"
    day_starts_at: int = 7
    night_starts_at: int = 19


@dataclass(frozen=True, slots=True)
class ThemePreferences:
    selected_theme_id: str = "builtin.default-dark"
    favorites: tuple[str, ...] = ()
    recently_used: tuple[str, ...] = ()
    random_theme_each_launch: bool = False
    policy: ThemePolicy = ThemePolicy.EXPLICIT
    high_contrast: bool = False
    monochrome: bool = False
    reduced_glare: bool = False


def _parse_hex(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise ThemeValidationError(f"Expected #RRGGBB colour, got {value!r}")
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _hex(red: int, green: int, blue: int) -> str:
    return f"#{red:02x}{green:02x}{blue:02x}"


def _hsl(hue: float, saturation: float, lightness: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(
        (hue % 360.0) / 360.0,
        max(0.0, min(1.0, lightness)),
        max(0.0, min(1.0, saturation)),
    )
    return _hex(round(red * 255), round(green * 255), round(blue * 255))


def _blend(first: str, second: str, amount: float) -> str:
    first_rgb = _parse_hex(first)
    second_rgb = _parse_hex(second)
    ratio = max(0.0, min(1.0, amount))
    return _hex(*(round(a + ((b - a) * ratio)) for a, b in zip(first_rgb, second_rgb, strict=True)))


def relative_luminance(color: str) -> float:
    """Return WCAG relative luminance for a ``#RRGGBB`` colour."""

    def linear(component: int) -> float:
        value = component / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = _parse_hex(color)
    return (0.2126 * linear(red)) + (0.7152 * linear(green)) + (0.0722 * linear(blue))


def contrast_ratio(first: str, second: str) -> float:
    """Return the WCAG contrast ratio between two colours."""

    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _foreground_for(background: str, minimum: float = 4.5) -> str:
    candidates = ("#ffffff", "#f7f7f8", "#111111", "#000000")
    passing = [color for color in candidates if contrast_ratio(color, background) >= minimum]
    if not passing:
        return max(candidates, key=lambda color: contrast_ratio(color, background))
    return max(passing, key=lambda color: contrast_ratio(color, background))


def _accessible_hue(
    hue: float,
    background: str,
    *,
    saturation: float = 0.78,
    minimum: float = 3.0,
) -> str:
    """Choose a vivid colour at the requested hue that clears ``minimum``."""

    dark_background = relative_luminance(background) < 0.35
    levels = (
        (0.68, 0.74, 0.80, 0.86, 0.92, 0.97)
        if dark_background
        else (0.40, 0.34, 0.28, 0.22, 0.16, 0.10)
    )
    choices = [_hsl(hue, saturation, level) for level in levels]
    passing = [color for color in choices if contrast_ratio(color, background) >= minimum]
    return (
        passing[0] if passing else max(choices, key=lambda color: contrast_ratio(color, background))
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "theme"


def _safe_text(value: object, *, field: str, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise ThemeValidationError(f"{field} must be text")
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ThemeValidationError(f"{field} is empty, too long, or contains controls")
    return value


_STYLE_OPTIONS: Final[dict[str, frozenset[str]]] = {
    "border_style": frozenset({"none", "solid", "double", "heavy", "rounded", "dashed"}),
    "divider_style": frozenset({"none", "solid", "double", "heavy", "dashed"}),
    "selection_style": frozenset({"filled", "outline", "accent-bar", "reverse"}),
    "row_density": frozenset({"compact", "comfortable", "spacious"}),
    "banner_treatment": frozenset({"flat", "outlined", "elevated", "subtle"}),
    "surface_elevation": frozenset({"flat", "subtle", "strong"}),
    "accent_intensity": frozenset({"subtle", "balanced", "vivid"}),
}


def validate_theme(theme: ThemeDefinition) -> tuple[ThemeIssue, ...]:
    """Return structural and accessibility issues for a theme definition."""

    issues: list[ThemeIssue] = []
    if theme.schema_version != THEME_SCHEMA_VERSION:
        issues.append(
            ThemeIssue(ValidationSeverity.ERROR, "schema_version", "Unsupported theme schema")
        )
    if not _ID_RE.fullmatch(theme.id):
        issues.append(ThemeIssue(ValidationSeverity.ERROR, "id", "Invalid stable theme ID"))
    if not theme.name or len(theme.name) > 120 or any(ord(char) < 32 for char in theme.name):
        issues.append(ThemeIssue(ValidationSeverity.ERROR, "name", "Invalid theme name"))
    if not theme.categories or any(not category.strip() for category in theme.categories):
        issues.append(
            ThemeIssue(ValidationSeverity.ERROR, "categories", "At least one category is required")
        )
    for field_name, allowed in _STYLE_OPTIONS.items():
        if getattr(theme.style, field_name) not in allowed:
            issues.append(
                ThemeIssue(ValidationSeverity.ERROR, field_name, "Unsupported structural style")
            )
    for name, color in theme.palette.colors():
        if not isinstance(color, str) or not _HEX_RE.fullmatch(color):
            issues.append(ThemeIssue(ValidationSeverity.ERROR, name, "Invalid #RRGGBB colour"))
    if len(theme.palette.calendar_event_colors) < 4:
        issues.append(
            ThemeIssue(
                ValidationSeverity.ERROR,
                "calendar_event_colors",
                "At least four distinguishable calendar colours are required",
            )
        )
    if any(issue.severity is ValidationSeverity.ERROR for issue in issues):
        return tuple(issues)

    surfaces = (
        "app_background",
        "sidebar_background",
        "message_list_background",
        "reading_pane_background",
        "modal_background",
        "command_palette_background",
    )
    required_text_ratio = 7.0 if theme.contrast is ContrastClass.MAXIMUM else 4.5
    for surface_name in surfaces:
        surface = getattr(theme.palette, surface_name)
        ratio = contrast_ratio(theme.palette.primary_text, surface)
        if ratio < required_text_ratio:
            issues.append(
                ThemeIssue(
                    ValidationSeverity.ERROR,
                    f"primary_text/{surface_name}",
                    f"Contrast {ratio:.2f}:1 is below {required_text_ratio:.1f}:1",
                )
            )
    selection_ratio = contrast_ratio(
        theme.palette.selection_foreground, theme.palette.selection_background
    )
    if selection_ratio < 4.5:
        issues.append(
            ThemeIssue(
                ValidationSeverity.ERROR,
                "selection",
                f"Selection contrast {selection_ratio:.2f}:1 is below 4.5:1",
            )
        )
    for name in ("focus_ring", "error", "warning"):
        ratio = contrast_ratio(getattr(theme.palette, name), theme.palette.app_background)
        if ratio < 3.0:
            issues.append(
                ThemeIssue(
                    ValidationSeverity.ERROR,
                    name,
                    f"Non-text contrast {ratio:.2f}:1 is below 3:1",
                )
            )
    muted_ratio = contrast_ratio(theme.palette.muted_text, theme.palette.app_background)
    if muted_ratio < 4.5:
        issues.append(
            ThemeIssue(
                ValidationSeverity.WARNING,
                "muted_text",
                f"Muted text contrast {muted_ratio:.2f}:1 is below 4.5:1",
            )
        )
    return tuple(issues)


def require_valid_theme(theme: ThemeDefinition) -> ThemeDefinition:
    errors = [
        issue for issue in validate_theme(theme) if issue.severity is ValidationSeverity.ERROR
    ]
    if errors:
        detail = "; ".join(f"{issue.field}: {issue.message}" for issue in errors)
        raise ThemeValidationError(detail)
    return theme


# The names below are product presets, grouped the same way as the Settings browser.
# Recipes vary hue, chroma, neutral temperature, surface spacing, structure, and
# contrast using a stable digest.  Keeping names as data makes the catalog cheap to
# construct and straightforward to audit against the product specification.
_FAMILY_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "core": (
        "Default Dark",
        "Default Light",
        "Classic Dark",
        "Classic Light",
        "Soft Dark",
        "Soft Light",
        "Modern Dark",
        "Modern Light",
        "Deep Dark",
        "Clean Light",
        "Minimal Dark",
        "Minimal Light",
        "Neutral Dark",
        "Neutral Light",
        "Balanced Dark",
        "Balanced Light",
        "Dimmed Dark",
        "Bright Light",
        "Paper",
        "Ink",
        "Graphite",
        "Charcoal",
        "Slate",
        "Smoke",
        "Carbon",
        "Obsidian",
        "Onyx",
        "Midnight",
        "Night",
        "Nightfall",
        "Deep Night",
        "Blackout",
        "True Black",
        "OLED Black",
        "Near Black",
        "Frost",
        "Snow",
        "Porcelain",
        "Ivory",
        "Pearl",
        "Cloud",
        "Silver",
    ),
    "high-contrast": (
        "High Contrast Dark",
        "High Contrast Light",
        "Maximum Contrast Dark",
        "Maximum Contrast Light",
        "Black and White",
        "White and Black",
        "High Contrast Yellow",
        "High Contrast Cyan",
        "High Contrast Green",
        "High Contrast Magenta",
        "High Contrast Orange",
        "Accessible Blue Dark",
        "Accessible Blue Light",
        "Accessible Green Dark",
        "Accessible Green Light",
        "Color-Blind Friendly Dark",
        "Color-Blind Friendly Light",
        "Deuteranopia-Friendly",
        "Protanopia-Friendly",
        "Tritanopia-Friendly",
        "Reduced Saturation",
        "Reduced Glare",
        "Low Vision Dark",
        "Low Vision Light",
    ),
    "monochrome": (
        "Monochrome Dark",
        "Monochrome Light",
        "Monochrome Gray",
        "Monochrome Blue",
        "Monochrome Green",
        "Monochrome Amber",
        "Monochrome Red",
        "Monochrome Purple",
        "Monochrome Cyan",
        "Terminal Monochrome",
        "Newspaper",
        "Typewriter",
        "Photocopier",
        "E-Ink",
        "Green Screen",
        "Amber Screen",
        "White Phosphor",
        "Blue Phosphor",
    ),
    "guc-germany": (
        "GUC Classic",
        "GUC Dark",
        "GUC Light",
        "GUC Black",
        "GUC Gold",
        "GUC Red",
        "GUC Flag",
        "GUC Modern",
        "GUC Midnight",
        "GUC High Contrast",
        "GUC Minimal",
        "GUC Alumni",
        "GUC Engineering",
        "GUC Campus Night",
        "Germany Classic",
        "German Flag Dark",
        "German Flag Light",
        "Bundesfarben",
        "Schwarz Rot Gold",
        "Schwarz Rot Gold Dark",
        "Schwarz Rot Gold Light",
        "Berlin Night",
        "Berlin Day",
        "Berlin U-Bahn",
        "Berlin Techno",
        "Berlin Concrete",
        "Berlin Neon",
        "Berlin Winter",
        "Munich",
        "Munich Night",
        "Hamburg",
        "Frankfurt",
        "Cologne",
        "Dresden",
        "Bauhaus",
        "Bauhaus Dark",
        "Bauhaus Light",
        "German Industrial",
        "German Minimalism",
        "Autobahn Night",
    ),
    "egypt-cairo": (
        "Cairo Night",
        "Cairo Day",
        "Cairo Sunset",
        "Cairo Concrete",
        "Cairo Neon",
        "Nile",
        "Nile Night",
        "Nile Blue",
        "Egyptian Gold",
        "Egyptian Sand",
        "Desert",
        "Desert Night",
        "Sahara",
        "Sahara Sunset",
        "Pharaoh",
        "Papyrus",
        "Alexandria",
        "Alexandria Sea",
        "North Coast",
        "Mediterranean Egypt",
        "Red Sea",
        "Giza Night",
    ),
    "blue": (
        "Blue",
        "Blue Dark",
        "Blue Light",
        "Deep Blue",
        "Navy",
        "Midnight Blue",
        "Royal Blue",
        "Electric Blue",
        "Cobalt",
        "Sapphire",
        "Azure",
        "Cerulean",
        "Sky Blue",
        "Ice Blue",
        "Steel Blue",
        "Denim",
        "Ocean",
        "Ocean Dark",
        "Atlantic",
        "Arctic Blue",
        "Blueberry",
        "Windows Blue",
        "Corporate Blue",
        "Academic Blue",
    ),
    "purple": (
        "Purple",
        "Purple Dark",
        "Purple Light",
        "Deep Purple",
        "Royal Purple",
        "Violet",
        "Electric Violet",
        "Lavender",
        "Lilac",
        "Plum",
        "Grape",
        "Amethyst",
        "Orchid",
        "Aubergine",
        "Midnight Purple",
        "Neon Purple",
        "Purple Haze",
        "Cosmic Purple",
        "Imperial Purple",
    ),
    "magenta-pink": (
        "Magenta",
        "Magenta Dark",
        "Magenta Light",
        "Hot Magenta",
        "Neon Magenta",
        "Fuchsia",
        "Rose",
        "Rose Dark",
        "Rose Light",
        "Pink",
        "Pink Dark",
        "Pink Light",
        "Hot Pink",
        "Bubblegum",
        "Flamingo",
        "Sakura",
        "Cherry Blossom",
        "Raspberry",
        "Cranberry",
        "Watermelon",
        "Barbie-inspired Pink",
        "Cyber Pink",
    ),
    "red": (
        "Red",
        "Red Dark",
        "Red Light",
        "Deep Red",
        "Crimson",
        "Scarlet",
        "Ruby",
        "Burgundy",
        "Wine",
        "Maroon",
        "Cherry",
        "Blood Red",
        "Fire",
        "Ember",
        "Lava",
        "Neon Red",
        "Racing Red",
        "Ferrari-inspired Red",
    ),
    "orange": (
        "Orange",
        "Orange Dark",
        "Orange Light",
        "Burnt Orange",
        "Tangerine",
        "Amber",
        "Amber Dark",
        "Amber Light",
        "Copper",
        "Bronze",
        "Pumpkin",
        "Sunset Orange",
        "Fire Orange",
        "Rust",
        "Terracotta",
        "Peach",
        "Apricot",
        "Neon Orange",
    ),
    "yellow-gold": (
        "Yellow",
        "Yellow Dark",
        "Yellow Light",
        "Gold",
        "Gold Dark",
        "Gold Light",
        "Champagne",
        "Honey",
        "Mustard",
        "Lemon",
        "Sunflower",
        "Solar",
        "Golden Hour",
        "Sand Gold",
        "Antique Gold",
        "Neon Yellow",
        "Warning Yellow",
    ),
    "green": (
        "Green",
        "Green Dark",
        "Green Light",
        "Forest",
        "Forest Dark",
        "Emerald",
        "Jade",
        "Mint",
        "Mint Dark",
        "Lime",
        "Olive",
        "Moss",
        "Sage",
        "Pine",
        "Matrix Green",
        "Terminal Green",
        "Neon Green",
        "Cyber Green",
        "Sea Green",
        "Racing Green",
        "British Racing Green",
    ),
    "cyan-teal": (
        "Cyan",
        "Cyan Dark",
        "Cyan Light",
        "Teal",
        "Teal Dark",
        "Teal Light",
        "Aqua",
        "Turquoise",
        "Lagoon",
        "Caribbean",
        "Tiffany-inspired",
        "Neon Cyan",
        "Electric Cyan",
        "Arctic Cyan",
        "Deep Teal",
    ),
    "earth-warm": (
        "Warm",
        "Warm Dark",
        "Warm Light",
        "Earth",
        "Earth Dark",
        "Earth Light",
        "Coffee",
        "Espresso",
        "Mocha",
        "Cappuccino",
        "Latte",
        "Cocoa",
        "Chocolate",
        "Caramel",
        "Cinnamon",
        "Sepia",
        "Leather",
        "Walnut",
        "Mahogany",
        "Clay",
        "Autumn",
        "Autumn Night",
        "Maple",
        "Sandstone",
        "Desert Warm",
        "Campfire",
        "Fireplace",
    ),
    "cool": (
        "Cool Dark",
        "Cool Light",
        "Glacier",
        "Glacier Dark",
        "Ice",
        "Ice Dark",
        "Arctic",
        "Arctic Night",
        "Winter",
        "Winter Night",
        "Frozen",
        "Frosted Glass",
        "Polar",
        "Nordic",
        "Nordic Night",
        "Nordic Day",
        "Scandinavian Dark",
        "Scandinavian Light",
    ),
    "pastel": (
        "Pastel Dark",
        "Pastel Light",
        "Pastel Rainbow",
        "Pastel Blue",
        "Pastel Purple",
        "Pastel Pink",
        "Pastel Green",
        "Pastel Yellow",
        "Pastel Orange",
        "Pastel Mint",
        "Pastel Lavender",
        "Pastel Peach",
        "Cotton Candy",
        "Macaron",
        "Soft Candy",
        "Spring",
        "Spring Morning",
    ),
    "neon-cyber": (
        "Neon",
        "Neon Dark",
        "Cyberpunk",
        "Cyberpunk 2077-inspired",
        "Cyber Neon",
        "Neon City",
        "Neon Nights",
        "Synthwave",
        "Synthwave Dark",
        "Retrowave",
        "Vaporwave",
        "Outrun",
        "Miami Vice-inspired",
        "Laser",
        "Arcade",
        "RGB",
        "Electric",
        "Holographic",
        "Ultraviolet",
        "Hyperblue",
        "Hyperpink",
        "Nightclub",
        "Techno",
        "Rave",
    ),
    "retro": (
        "DOS",
        "DOS Blue",
        "DOS Amber",
        "IBM Terminal",
        "IBM Blue",
        "IBM Green",
        "VT100",
        "VT220",
        "CRT Green",
        "CRT Amber",
        "CRT White",
        "CRT Blue",
        "Macintosh Classic",
        "Macintosh Platinum",
        "Windows 95",
        "Windows 98",
        "Windows XP",
        "Windows XP Olive",
        "Windows XP Silver",
        "Windows XP Royale",
        "Windows Vista",
        "Windows 7",
        "Classic Unix",
        "XTerm",
        "Linux Console",
        "Commodore 64",
        "Amiga",
        "Apple II",
        "ZX Spectrum",
        "Hacker Terminal",
        "Mainframe",
    ),
    "modern-os": (
        "macOS Dark",
        "macOS Light",
        "macOS Graphite",
        "macOS Midnight",
        "iOS Dark",
        "iOS Light",
        "Windows 11 Dark",
        "Windows 11 Light",
        "Windows Terminal",
        "GNOME Dark",
        "GNOME Light",
        "KDE Breeze Dark",
        "KDE Breeze Light",
        "Ubuntu Yaru Dark",
        "Ubuntu Yaru Light",
        "Material Dark",
        "Material Light",
        "Material You Dark",
        "Material You Light",
        "Fluent Dark",
        "Fluent Light",
    ),
    "developer": (
        "Dracula",
        "Dracula Soft",
        "Nord",
        "Nord Light",
        "Gruvbox Dark",
        "Gruvbox Light",
        "Gruvbox Material",
        "Solarized Dark",
        "Solarized Light",
        "One Dark",
        "One Light",
        "Monokai",
        "Monokai Pro-inspired",
        "GitHub Dark",
        "GitHub Light",
        "GitHub Dimmed",
        "GitLab Dark",
        "VS Code Dark",
        "VS Code Light",
        "Visual Studio Dark",
        "Visual Studio Blue",
        "Atom Dark",
        "Sublime Dark",
        "Ayu Dark",
        "Ayu Mirage",
        "Ayu Light",
        "Tokyo Night",
        "Tokyo Night Storm",
        "Tokyo Night Moon",
        "Catppuccin Latte",
        "Catppuccin Frappe",
        "Catppuccin Macchiato",
        "Catppuccin Mocha",
        "Everforest Dark",
        "Everforest Light",
        "Kanagawa",
        "Rose Pine",
        "Rose Pine Moon",
        "Rose Pine Dawn",
        "Night Owl",
        "Cobalt2-inspired",
        "Horizon",
        "Material Palenight",
        "Moonlight",
    ),
    "low-light": (
        "Low Contrast Night",
        "Ultra Dim",
        "Bedtime",
        "Midnight Dim",
        "Moonless",
        "Deep Space",
        "Black Velvet",
        "Darkroom",
        "Cinema",
        "Night Reader",
        "Red Shift",
        "Amber Night",
        "Warm Night",
        "Blue Light Reduced",
        "Eye Comfort",
        "OLED Night",
        "Stealth",
        "Shadow",
    ),
    "space": (
        "Space",
        "Cosmos",
        "Galaxy",
        "Milky Way",
        "Nebula",
        "Orion",
        "Andromeda",
        "Supernova",
        "Black Hole",
        "Event Horizon",
        "Lunar",
        "Moon",
        "Mars",
        "Mars Night",
        "Jupiter",
        "Saturn",
        "Venus",
        "Mercury",
        "Neptune",
        "Pluto",
        "Starfield",
        "NASA-inspired",
        "Spaceship",
        "Command Deck",
        "Sci-Fi Terminal",
        "Alien",
        "Xeno",
        "Quantum",
    ),
    "nature": (
        "Rainforest",
        "Jungle",
        "Fern",
        "Mountain",
        "Alpine",
        "River",
        "Lake",
        "Deep Ocean",
        "Coral Reef",
        "Beach",
        "Sunset Beach",
        "Sunrise",
        "Sunset",
        "Dawn",
        "Dusk",
        "Twilight",
        "Aurora",
        "Aurora Borealis",
        "Storm",
        "Rain",
        "Thunderstorm",
        "Fog",
        "Mist",
        "Volcano",
        "Canyon",
        "Savannah",
        "Meadow",
        "Spring Garden",
        "Autumn Forest",
    ),
    "seasonal": (
        "Spring Night",
        "Summer",
        "Summer Night",
        "Christmas Dark",
        "Christmas Light",
        "Halloween",
        "Halloween Dark",
        "Ramadan Night",
        "Ramadan Lantern",
        "Eid",
        "New Year",
        "Valentine",
        "Valentine Dark",
    ),
    "luxury": (
        "Luxury Black",
        "Black and Gold",
        "Black and Silver",
        "Black and Red",
        "Black and Purple",
        "Black and Emerald",
        "Platinum",
        "Titanium",
        "Chrome",
        "Steel",
        "Brushed Metal",
        "Carbon Fiber",
        "Rose Gold",
        "Marble",
        "White Marble",
        "Black Marble",
        "Granite",
        "Velvet",
        "Satin",
        "Piano Black",
    ),
    "academic": (
        "Academic",
        "University",
        "Research",
        "Laboratory",
        "Engineering",
        "Computer Science",
        "Mathematics",
        "Physics",
        "Library",
        "Manuscript",
        "Journal",
        "Thesis",
        "Blackboard",
        "Whiteboard",
        "Blueprint",
        "Architect",
        "Corporate",
        "Executive",
        "Office",
        "Formal",
        "Professional Blue",
        "Professional Gray",
    ),
    "minimal": (
        "Minimal",
        "Minimal Gray",
        "Minimal Blue",
        "Minimal Warm",
        "Zen",
        "Zen Dark",
        "Zen Light",
        "Quiet",
        "Calm",
        "Focus",
        "Distraction Free",
        "Clean Room",
        "Bare",
        "Flat",
        "Soft Flat",
    ),
    "playful": (
        "Rainbow",
        "Rainbow Dark",
        "Prism",
        "Candy",
        "Candy Dark",
        "Fruit Punch",
        "Tropical",
        "Tropical Night",
        "Fiesta",
        "Confetti",
        "Arcade Pop",
        "Comic",
        "Pop Art",
        "Jelly",
        "Gummy",
        "Toybox",
    ),
    "special-purpose": (
        "Reading Mode Dark",
        "Reading Mode Light",
        "Writing Mode",
        "Coding Mode",
        "Search Mode",
        "Inbox Zero",
        "Focus Inbox",
        "Attachment Review",
        "Calendar Focus",
        "Meeting Mode",
        "Backup Console",
        "Server Monitor",
        "Diagnostics",
        "Privacy Mode",
        "Presentation Mode",
        "Compact Terminal",
        "Tiny Terminal",
        "Ultrawide",
    ),
}


_FAMILY_HUES: Final[dict[str, float]] = {
    "core": 216.0,
    "high-contrast": 205.0,
    "monochrome": 210.0,
    "guc-germany": 4.0,
    "egypt-cairo": 38.0,
    "blue": 216.0,
    "purple": 274.0,
    "magenta-pink": 328.0,
    "red": 4.0,
    "orange": 27.0,
    "yellow-gold": 46.0,
    "green": 137.0,
    "cyan-teal": 181.0,
    "earth-warm": 24.0,
    "cool": 200.0,
    "pastel": 292.0,
    "neon-cyber": 292.0,
    "retro": 126.0,
    "modern-os": 210.0,
    "developer": 252.0,
    "low-light": 25.0,
    "space": 251.0,
    "nature": 132.0,
    "seasonal": 350.0,
    "luxury": 43.0,
    "academic": 213.0,
    "minimal": 205.0,
    "playful": 318.0,
    "special-purpose": 196.0,
}


_LIGHT_HINTS: Final = frozenset(
    {
        "light",
        "day",
        "paper",
        "snow",
        "porcelain",
        "ivory",
        "pearl",
        "cloud",
        "silver",
        "frost",
        "newspaper",
        "typewriter",
        "photocopier",
        "e-ink",
        "white",
        "morning",
        "latte",
        "peach",
        "apricot",
        "mint",
        "lavender",
        "macaron",
        "spring",
        "clean",
        "bright",
        "platinum",
        "marble",
        "whiteboard",
        "presentation",
        "reading mode light",
        "one light",
        "ayu light",
        "github light",
        "nord light",
        "solarized light",
        "catppuccin latte",
        "rose pine dawn",
    }
)


def _infer_mode(name: str, family: str, ordinal: int) -> ThemeMode:
    lowered = name.casefold()
    if lowered == "white and black":
        return ThemeMode.LIGHT
    if lowered in {"green screen", "amber screen", "white phosphor", "blue phosphor"}:
        return ThemeMode.DARK
    if "dark" in lowered or "night" in lowered or "black" in lowered or "midnight" in lowered:
        return ThemeMode.DARK
    if any(hint in lowered for hint in _LIGHT_HINTS):
        return ThemeMode.LIGHT
    if family in {"neon-cyber", "low-light", "space", "retro"}:
        return ThemeMode.DARK
    return ThemeMode.DARK if ordinal % 3 else ThemeMode.LIGHT


def _contrast_class(name: str, family: str) -> ContrastClass:
    lowered = name.casefold()
    if "maximum contrast" in lowered or lowered in {"black and white", "white and black"}:
        return ContrastClass.MAXIMUM
    if family == "high-contrast" or "high contrast" in lowered or "low vision" in lowered:
        return ContrastClass.HIGH
    if family == "low-light" or "reduced glare" in lowered or "dim" in lowered:
        return ContrastClass.REDUCED_GLARE
    return ContrastClass.STANDARD


def _tint_at_luminance(color: str, hue: float, saturation: float) -> str:
    """Keep tonal contrast while projecting a semantic color onto one hue."""

    target = relative_luminance(color)
    low, high = 0.0, 1.0
    for _ in range(20):
        middle = (low + high) / 2
        candidate = _hsl(hue, saturation, middle)
        if relative_luminance(candidate) < target:
            low = middle
        else:
            high = middle
    candidates = (_hsl(hue, saturation, low), _hsl(hue, saturation, high))
    return min(candidates, key=lambda value: abs(relative_luminance(value) - target))


def _monochrome_palette(name: str, palette: ThemePalette) -> ThemePalette:
    """Use neutral or named phosphor tones for every role, including status."""

    lowered = name.casefold()
    hues = {
        "blue": 210.0,
        "green": 128.0,
        "amber": 43.0,
        "red": 3.0,
        "purple": 280.0,
        "cyan": 185.0,
    }
    hue = next((value for token, value in hues.items() if token in lowered), 0.0)
    saturation = 0.55 if any(token in lowered for token in hues) else 0.0
    result = _map_palette(palette, lambda value: _tint_at_luminance(value, hue, saturation))
    dark = relative_luminance(result.app_background) < 0.35
    levels = (0.60, 0.66, 0.72, 0.78, 0.84, 0.90) if dark else (0.34, 0.29, 0.24, 0.19, 0.14, 0.09)
    # Separate events through six tonal levels rather than reintroducing hues.
    result = replace(
        result, calendar_event_colors=tuple(_hsl(hue, saturation, level) for level in levels)
    )
    if contrast_ratio(result.muted_text, result.app_background) < 4.5:
        result = replace(
            result,
            muted_text=_accessible_hue(
                hue, result.app_background, saturation=saturation, minimum=4.6
            ),
        )
    return result


def _palette_for(name: str, family: str, ordinal: int) -> ThemePalette:
    digest = hashlib.sha256(f"{family}\0{name}\0{ordinal}".encode()).digest()
    mode = _infer_mode(name, family, ordinal)
    hue = (_FAMILY_HUES[family] + ((digest[0] / 255.0) * 42.0) - 21.0) % 360.0
    temperature = ((digest[1] / 255.0) * 16.0) - 8.0
    neutral_saturation = 0.025 + ((digest[2] / 255.0) * 0.15)
    reduced = family in {"minimal", "monochrome", "low-light"} or "reduced" in name.casefold()
    vivid = family in {"neon-cyber", "playful"} or "neon" in name.casefold()
    accent_saturation = 0.34 if reduced else (0.95 if vivid else 0.72 + digest[3] / 2550.0)

    if mode is ThemeMode.DARK:
        base_lightness = 0.025 + (digest[4] / 255.0) * (0.055 if family == "low-light" else 0.09)
        if any(word in name.casefold() for word in ("true black", "oled", "blackout")):
            base_lightness = 0.0
        app = _hsl(hue + temperature, neutral_saturation, base_lightness)
        primary = "#ffffff" if _contrast_class(name, family) is ContrastClass.MAXIMUM else "#f5f7fa"
        neutral_target = "#ffffff"
        secondary = _blend(primary, app, 0.20)
        muted = _blend(primary, app, 0.34)
        disabled = _blend(primary, app, 0.52)
        surface_steps = (0.035, 0.055, 0.075, 0.095, 0.115)
    else:
        base_lightness = 0.925 + (digest[4] / 255.0) * 0.065
        app = _hsl(hue + temperature, neutral_saturation * 0.8, base_lightness)
        primary = "#000000" if _contrast_class(name, family) is ContrastClass.MAXIMUM else "#15171a"
        neutral_target = "#000000"
        secondary = _blend(primary, app, 0.22)
        # Keep muted copy visually subordinate without dropping below WCAG AA
        # against the light app surface.  The former 0.40 blend left 64 pale
        # presets fractionally below 4.5:1 after RGB rounding.
        muted = _blend(primary, app, 0.38)
        disabled = _blend(primary, app, 0.58)
        surface_steps = (-0.025, -0.04, -0.055, -0.07, -0.085)

    def surface(step: float, hue_shift: float = 0.0) -> str:
        return _hsl(hue + temperature + hue_shift, neutral_saturation, base_lightness + step)

    accent = _accessible_hue(hue, app, saturation=accent_saturation, minimum=3.2)
    accent2 = _accessible_hue(hue + 48.0 + digest[5] / 8.0, app, saturation=accent_saturation)
    accent3 = _accessible_hue(hue + 174.0 + digest[6] / 8.0, app, saturation=accent_saturation)
    selection_foreground = _foreground_for(accent, 4.5)
    success = _accessible_hue(142.0, app, saturation=0.68)
    information = _accessible_hue(205.0, app, saturation=0.76)
    warning = _accessible_hue(43.0, app, saturation=0.86)
    error = _accessible_hue(3.0, app, saturation=0.82)
    header = surface(surface_steps[3], -temperature / 2.0)
    calendar = tuple(
        _accessible_hue(hue + offset, app, saturation=0.66, minimum=3.0)
        for offset in (0.0, 57.0, 119.0, 181.0, 239.0, 301.0)
    )

    if family == "monochrome":
        # Preserve the monochrome character while varying tone spacing per preset.
        saturation = 0.0 if "blue" not in name.casefold() else 0.18
        mono_hue = 210.0 if "blue" in name.casefold() else hue
        accent = _accessible_hue(mono_hue, app, saturation=saturation, minimum=3.2)
        accent2 = _blend(accent, neutral_target, 0.12 + digest[7] / 1800.0)
        accent3 = _blend(accent, app, 0.16 + digest[8] / 1800.0)

    palette = ThemePalette(
        app_background=app,
        secondary_background=surface(surface_steps[0]),
        sidebar_background=surface(surface_steps[1], -5.0),
        message_list_background=surface(surface_steps[0], 3.0),
        reading_pane_background=surface(surface_steps[0] / 2.0, 7.0),
        elevated_surface=surface(surface_steps[3]),
        command_palette_background=surface(surface_steps[4], 5.0),
        modal_background=surface(surface_steps[3], -4.0),
        primary_text=primary,
        secondary_text=secondary,
        muted_text=muted,
        disabled_text=disabled,
        primary_accent=accent,
        secondary_accent=accent2,
        tertiary_accent=accent3,
        border=_blend(accent, app, 0.42),
        divider=_blend(accent2, app, 0.50),
        focus_ring=accent,
        hover=_blend(accent, app, 0.80),
        active=_blend(accent, app, 0.66),
        selection_background=accent,
        selection_foreground=selection_foreground,
        unread_message=primary,
        read_message=secondary,
        flagged=warning,
        important=error,
        link=information,
        success=success,
        information=information,
        warning=warning,
        error=error,
        quota_safe=success,
        quota_warning=warning,
        quota_critical=error,
        calendar_event_colors=calendar,
        contact_highlight=_blend(accent3, app, 0.78),
        search_highlight=_blend(warning, app, 0.76),
        command_result_highlight=_blend(accent, app, 0.74),
        logo_primary=_accessible_hue(hue, header, saturation=accent_saturation, minimum=3.0),
        logo_secondary=_accessible_hue(3.0, header, saturation=0.85, minimum=3.0),
        logo_accent=_accessible_hue(46.0, header, saturation=0.88, minimum=3.0),
        scrollbar_track=surface(surface_steps[1]),
        scrollbar_thumb=_blend(accent, app, 0.34),
        footer=surface(surface_steps[2], 4.0),
        header=header,
    )
    # The everyday defaults use a deliberate neutral hierarchy. Other presets
    # retain their family recipes and stable identifiers.
    if name == "Default Dark":
        return replace(
            palette,
            app_background="#101010",
            secondary_background="#181818",
            sidebar_background="#101010",
            message_list_background="#101010",
            reading_pane_background="#101010",
            elevated_surface="#1c1c1c",
            command_palette_background="#101010",
            modal_background="#181818",
            primary_text="#eee9dc",
            secondary_text="#b5b1a7",
            muted_text="#949494",
            disabled_text="#777777",
            primary_accent="#f0c75e",
            secondary_accent="#f15b57",
            tertiary_accent="#f0c75e",
            border="#4b4434",
            divider="#383838",
            focus_ring="#f0c75e",
            hover="#20201c",
            active="#29251b",
            selection_background="#29251b",
            selection_foreground="#f0c75e",
            unread_message="#eee9dc",
            read_message="#b5b1a7",
            flagged="#f0c75e",
            important="#f18a78",
            link="#e5c879",
            success="#a7c59a",
            information="#c6c2b7",
            warning="#f0c75e",
            error="#f18a78",
            logo_primary="#f15b57",
            logo_secondary="#eee9dc",
            logo_accent="#f0c75e",
            scrollbar_track="#101010",
            scrollbar_thumb="#4b4b46",
            footer="#101010",
            header="#101010",
        )
    if name == "Default Light":
        return replace(
            palette,
            app_background="#f6f7fa",
            secondary_background="#eef1f6",
            sidebar_background="#eef1f6",
            message_list_background="#ffffff",
            reading_pane_background="#ffffff",
            elevated_surface="#e5ecf5",
            command_palette_background="#f9fbff",
            modal_background="#f9fbff",
            primary_text="#172b43",
            secondary_text="#3c526b",
            muted_text="#52677e",
            primary_accent="#23588e",
            secondary_accent="#256672",
            tertiary_accent="#23588e",
            border="#9bacbf",
            divider="#c9d3e0",
            focus_ring="#23588e",
            hover="#e5edf7",
            active="#d8e8f8",
            selection_background="#d8e8f8",
            selection_foreground="#172b43",
            unread_message="#172b43",
            read_message="#3c526b",
            scrollbar_track="#eef1f6",
            scrollbar_thumb="#7b8fa8",
            footer="#eef1f6",
            header="#eef1f6",
        )
    if family == "monochrome" or name.casefold() in {"black and white", "white and black"}:
        return _monochrome_palette(name, palette)
    return palette


def _style_for(family: str, ordinal: int, digest: bytes) -> ThemeStyle:
    if family == "core" and ordinal in (0, 1):
        return ThemeStyle(border_style="solid", divider_style="solid", selection_style="filled")
    border_choices = ("solid", "rounded", "double", "heavy", "dashed")
    divider_choices = ("solid", "double", "heavy", "dashed")
    density = (
        "compact"
        if family in {"retro", "special-purpose"}
        else ("spacious" if family in {"pastel", "minimal"} else "comfortable")
    )
    vivid = family in {"neon-cyber", "playful"}
    flat = family in {"minimal", "monochrome", "retro"}
    return ThemeStyle(
        border_style=border_choices[(ordinal + digest[9]) % len(border_choices)],
        divider_style=divider_choices[(ordinal + digest[10]) % len(divider_choices)],
        selection_style=("filled", "outline", "accent-bar", "reverse")[(ordinal + digest[11]) % 4],
        bold_text=family not in {"low-light", "minimal"} or ordinal % 2 == 0,
        row_density=density,
        banner_treatment="outlined" if family == "guc-germany" else ("subtle" if flat else "flat"),
        surface_elevation="flat" if flat else ("strong" if vivid else "subtle"),
        accent_intensity="vivid" if vivid else ("subtle" if flat else "balanced"),
    )


# Shared named presets remain one definition and appear in every declared family.
_SECONDARY_THEME_CATEGORIES: Final[dict[str, tuple[str, ...]]] = {
    "professional blue": ("professional",),
    "professional gray": ("professional",),
    "autumn": ("seasonal",),
    "autumn night": ("seasonal",),
    "bronze": ("luxury",),
    "bubblegum": ("playful",),
    "champagne": ("luxury",),
    "copper": ("luxury",),
    "deep space": ("space",),
    "forest": ("nature",),
    "glacier": ("nature",),
    "leather": ("luxury",),
    "minimal dark": ("minimal",),
    "minimal light": ("minimal",),
    "moonlight": ("low-light",),
    "moss": ("nature",),
    "ocean": ("nature",),
    "spring": ("seasonal",),
    "terracotta": ("earth-warm",),
    "winter": ("seasonal",),
    "winter night": ("seasonal",),
}


def _categories_for(name: str, family: str, mode: ThemeMode) -> tuple[str, ...]:
    categories = {"built-in", family, mode.value}
    categories.update(_SECONDARY_THEME_CATEGORIES.get(name.casefold(), ()))
    if family == "guc-germany":
        categories.add("guc-germany")
    if family == "retro":
        categories.add("terminal")
    if family == "high-contrast":
        categories.add("accessibility")
    if family == "monochrome":
        categories.add("accessibility")
    if family in {
        "blue",
        "purple",
        "magenta-pink",
        "red",
        "orange",
        "yellow-gold",
        "green",
        "cyan-teal",
    }:
        categories.add("color")
    if "terminal" in name.casefold() or any(
        token in name.casefold() for token in ("dos", "crt", "vt", "xterm")
    ):
        categories.add("terminal")
    return tuple(sorted(categories))


def _generate_builtin_themes() -> tuple[ThemeDefinition, ...]:
    """Authoritative deterministic recipe generation, also the resource fallback."""

    result: list[ThemeDefinition] = []
    used_names: set[str] = set()
    used_ids: set[str] = set()
    for family, names in _FAMILY_NAMES.items():
        for ordinal, name in enumerate(names):
            # A few names intentionally appear in multiple product families.  One
            # stable definition can carry multiple search categories; aliases never
            # inflate the built-in count.
            folded = name.casefold()
            if folded in used_names:
                continue
            used_names.add(folded)
            theme_id = f"builtin.{_slug(name)}"
            if theme_id in used_ids:
                theme_id = f"{theme_id}-{_slug(family)}"
            used_ids.add(theme_id)
            digest = hashlib.sha256(f"{family}\0{name}\0{ordinal}".encode()).digest()
            mode = _infer_mode(name, family, ordinal)
            contrast = _contrast_class(name, family)
            color_blind = (
                "color-blind" in folded
                or "deuteranopia" in folded
                or "protanopia" in folded
                or "tritanopia" in folded
            )
            theme = ThemeDefinition(
                id=theme_id,
                name=name,
                categories=_categories_for(name, family, mode),
                mode=mode,
                contrast=contrast,
                palette=_palette_for(name, family, ordinal),
                style=_style_for(family, ordinal, digest),
                provenance=ThemeProvenance(
                    source="generated-curated",
                    family=family,
                    recipe=f"{family}:{ordinal}",
                ),
                search_tags=tuple(
                    sorted(
                        {
                            family,
                            mode.value,
                            contrast.value,
                            *re.findall(r"[a-z0-9]+", folded),
                        }
                    )
                ),
                color_blind_friendly=color_blind,
            )
            result.append(require_valid_theme(theme))
    signatures = {theme.visual_signature for theme in result}
    if len(signatures) != len(result):
        raise RuntimeError("Built-in theme recipes produced duplicate visual systems")
    return tuple(result)


_BUILTIN_CATALOGUE_SHA256: Final = (
    "403192fa3946573294d69f8553ae5563ee9a7fd8d7ef6322fca28911d63946f9"
)
_BUILTIN_CATALOGUE_MAX_BYTES: Final = 512 * 1024
_BUILTIN_CATALOGUE_MAX_JSON: Final = 2 * 1024 * 1024


def _read_builtin_catalogue() -> bytes:
    from importlib.resources import files

    resource = files("gucmail").joinpath("data", "builtin-themes.json.gz")
    with resource.open("rb") as stream:
        return stream.read(_BUILTIN_CATALOGUE_MAX_BYTES + 1)


def _load_builtin_catalogue() -> tuple[ThemeDefinition, ...]:
    """Read a bounded, pinned built-in asset; custom imports use their own parser."""
    import gzip
    import io

    compressed = _read_builtin_catalogue()
    if (
        len(compressed) > _BUILTIN_CATALOGUE_MAX_BYTES
        or hashlib.sha256(compressed).hexdigest() != _BUILTIN_CATALOGUE_SHA256
    ):
        raise ThemeValidationError("Built-in catalogue integrity check failed")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        decoded = stream.read(_BUILTIN_CATALOGUE_MAX_JSON + 1)
    if len(decoded) > _BUILTIN_CATALOGUE_MAX_JSON:
        raise ThemeValidationError("Built-in catalogue exceeds its decoded bound")
    payload = json.loads(decoded)
    if (
        payload["schema"] != "gucmail.builtin-themes/v1"
        or payload["generator"] != GENERATOR_VERSION
        or len(payload["themes"]) != 661
    ):
        raise ThemeValidationError("Built-in catalogue schema or count differs")
    result = tuple(require_valid_theme(ThemeDefinition(
        id=raw["id"], name=raw["name"], categories=tuple(raw["categories"]),
        mode=ThemeMode(raw["mode"]), contrast=ContrastClass(raw["contrast"]),
        palette=ThemePalette(**{  # type: ignore[arg-type]
            key: tuple(value) if isinstance(value, list) else value
            for key, value in raw["palette"].items()
        }),
        style=ThemeStyle(**raw["style"]), provenance=ThemeProvenance(**raw["provenance"]),
        search_tags=tuple(raw["search_tags"]), color_blind_friendly=raw["color_blind_friendly"],
        schema_version=raw["schema_version"],
    )) for raw in payload["themes"])
    if len({theme.id for theme in result}) != len(result):
        raise ThemeValidationError("Built-in catalogue contains duplicate identities")
    return result


@lru_cache(maxsize=1)
def builtin_themes() -> tuple[ThemeDefinition, ...]:
    """Load validated immutable built-ins, regenerating faithfully if the asset fails."""
    import zlib

    try:
        return _load_builtin_catalogue()
    except (OSError, ValueError, TypeError, KeyError, EOFError, zlib.error):
        # A missing/corrupt optional optimization must not remove customization.
        return _generate_builtin_themes()


_ANSI_16: Final[tuple[str, ...]] = (
    "#000000",
    "#800000",
    "#008000",
    "#808000",
    "#000080",
    "#800080",
    "#008080",
    "#c0c0c0",
    "#808080",
    "#ff0000",
    "#00ff00",
    "#ffff00",
    "#0000ff",
    "#ff00ff",
    "#00ffff",
    "#ffffff",
)


@lru_cache(maxsize=1)
def _xterm_256() -> tuple[str, ...]:
    colors = list(_ANSI_16)
    levels = (0, 95, 135, 175, 215, 255)
    colors.extend(_hex(red, green, blue) for red in levels for green in levels for blue in levels)
    colors.extend(_hex(level, level, level) for level in range(8, 239, 10))
    return tuple(colors)


def _nearest(color: str, candidates: Sequence[str]) -> str:
    red, green, blue = _parse_hex(color)

    def distance(candidate: str) -> int:
        other_red, other_green, other_blue = _parse_hex(candidate)
        # Weighted Euclidean distance tracks perceived RGB differences better than
        # treating every channel equally and remains deterministic.
        return (
            (3 * (red - other_red) ** 2)
            + (4 * (green - other_green) ** 2)
            + (2 * (blue - other_blue) ** 2)
        )

    return min(candidates, key=distance)


def _map_palette(palette: ThemePalette, mapper: Callable[[str], str]) -> ThemePalette:
    values: dict[str, object] = {}
    for field in fields(palette):
        value = getattr(palette, field.name)
        values[field.name] = (
            tuple(mapper(item) for item in value) if isinstance(value, tuple) else mapper(value)
        )
    return ThemePalette(**values)  # type: ignore[arg-type]


def adapt_theme(theme: ThemeDefinition, capability: ColorCapability) -> ThemeDefinition:
    """Adapt a theme to true-colour, 256, 16, or monochrome terminals."""

    if capability is ColorCapability.TRUECOLOR:
        return theme
    if capability is ColorCapability.COLORS_256:
        palette = _map_palette(theme.palette, lambda color: _nearest(color, _xterm_256()))
    elif capability is ColorCapability.COLORS_16:
        palette = _map_palette(theme.palette, lambda color: _nearest(color, _ANSI_16))
    else:
        dark = theme.mode is ThemeMode.DARK
        background, foreground = ("#000000", "#ffffff") if dark else ("#ffffff", "#000000")
        surfaces = {
            "app_background",
            "secondary_background",
            "sidebar_background",
            "message_list_background",
            "reading_pane_background",
            "elevated_surface",
            "command_palette_background",
            "modal_background",
            "hover",
            "active",
            "scrollbar_track",
            "footer",
            "header",
        }

        def monochrome(name: str) -> str:
            return background if name in surfaces else foreground

        values: dict[str, object] = {}
        for field in fields(theme.palette):
            value = getattr(theme.palette, field.name)
            if isinstance(value, tuple):
                values[field.name] = tuple(foreground for _ in value)
            elif field.name == "selection_background":
                values[field.name] = foreground
            elif field.name == "selection_foreground":
                values[field.name] = background
            else:
                values[field.name] = monochrome(field.name)
        palette = ThemePalette(**values)  # type: ignore[arg-type]
    if capability in {ColorCapability.COLORS_256, ColorCapability.COLORS_16}:
        # Independent nearest-colour rounding can destroy a valid contrast pair.
        # Pick the nearest supported foreground that still meets the semantic ratio.
        colors = _xterm_256() if capability is ColorCapability.COLORS_256 else _ANSI_16

        def readable(color: str, backgrounds: tuple[str, ...], minimum: float) -> str:
            if all(contrast_ratio(color, background) >= minimum for background in backgrounds):
                return color
            candidates = tuple(
                candidate
                for candidate in colors
                if all(
                    contrast_ratio(candidate, background) >= minimum for background in backgrounds
                )
            )
            if not candidates:
                raise ThemeValidationError("Terminal palette cannot preserve theme contrast")
            if len(set(_parse_hex(color))) == 1:
                neutral = tuple(value for value in candidates if len(set(_parse_hex(value))) == 1)
                if neutral:
                    return _nearest(color, neutral)
            return _nearest(color, candidates)

        minimum_text = 7.0 if theme.contrast is ContrastClass.MAXIMUM else 4.5
        primary = readable(palette.primary_text, (palette.app_background,), minimum_text)
        surface_names = (
            "app_background",
            "sidebar_background",
            "message_list_background",
            "reading_pane_background",
            "modal_background",
            "command_palette_background",
        )
        # A rounded surface can cross the light/dark boundary (e.g. ANSI olive).
        # Repair that surface before choosing one foreground shared by all panes.
        palette = replace(
            palette,
            app_background=readable(palette.app_background, (primary,), minimum_text),
            sidebar_background=readable(palette.sidebar_background, (primary,), minimum_text),
            message_list_background=readable(
                palette.message_list_background, (primary,), minimum_text
            ),
            reading_pane_background=readable(
                palette.reading_pane_background, (primary,), minimum_text
            ),
            modal_background=readable(palette.modal_background, (primary,), minimum_text),
            command_palette_background=readable(
                palette.command_palette_background, (primary,), minimum_text
            ),
        )
        adapted_surfaces = tuple(getattr(palette, name) for name in surface_names)
        palette = replace(
            palette,
            primary_text=readable(
                palette.primary_text,
                adapted_surfaces,
                7.0 if theme.contrast is ContrastClass.MAXIMUM else 4.5,
            ),
            selection_foreground=readable(
                palette.selection_foreground, (palette.selection_background,), 4.5
            ),
            focus_ring=readable(palette.focus_ring, (palette.app_background,), 3.0),
            error=readable(palette.error, (palette.app_background,), 3.0),
            warning=readable(palette.warning, (palette.app_background,), 3.0),
            muted_text=readable(palette.muted_text, (palette.app_background,), 4.5),
        )
    return replace(
        theme,
        palette=palette,
        provenance=replace(
            theme.provenance, recipe=f"{theme.provenance.recipe}@{capability.value}"
        ),
    )


def adapt_theme_high_contrast(theme: ThemeDefinition) -> ThemeDefinition:
    """Apply a maximum-contrast semantic overlay without changing theme identity."""

    dark = theme.mode is ThemeMode.DARK
    if dark:
        background, surface, elevated = "#000000", "#080808", "#151515"
        foreground, secondary, muted, disabled = (
            "#ffffff",
            "#f2f2f2",
            "#d6d6d6",
            "#a8a8a8",
        )
        primary, secondary_accent, tertiary = "#00ffff", "#ffff00", "#ff66ff"
        hover, active = "#202020", "#303030"
        success, warning, error = "#66ff99", "#ffff00", "#ff7070"
        calendar = ("#00ffff", "#ffff00", "#ff66ff", "#66ff99", "#ff9f40", "#80aaff")
    else:
        background, surface, elevated = "#ffffff", "#f7f7f7", "#e9e9e9"
        foreground, secondary, muted, disabled = (
            "#000000",
            "#111111",
            "#303030",
            "#5c5c5c",
        )
        primary, secondary_accent, tertiary = "#0033cc", "#7a4b00", "#790079"
        hover, active = "#e4e4e4", "#d2d2d2"
        success, warning, error = "#006b2e", "#704500", "#a00000"
        calendar = ("#0033cc", "#7a4b00", "#790079", "#006b2e", "#9a3d00", "#005a70")

    palette = replace(
        theme.palette,
        app_background=background,
        secondary_background=surface,
        sidebar_background=surface,
        message_list_background=background,
        reading_pane_background=background,
        elevated_surface=elevated,
        command_palette_background=elevated,
        modal_background=elevated,
        primary_text=foreground,
        secondary_text=secondary,
        muted_text=muted,
        disabled_text=disabled,
        primary_accent=primary,
        secondary_accent=secondary_accent,
        tertiary_accent=tertiary,
        border=foreground,
        divider=muted,
        focus_ring=primary,
        hover=hover,
        active=active,
        selection_background=foreground,
        selection_foreground=background,
        unread_message=foreground,
        read_message=secondary,
        flagged=warning,
        important=error,
        link=primary,
        success=success,
        information=primary,
        warning=warning,
        error=error,
        quota_safe=success,
        quota_warning=warning,
        quota_critical=error,
        calendar_event_colors=calendar,
        contact_highlight=primary,
        search_highlight=warning,
        command_result_highlight=primary,
        logo_primary=primary,
        logo_secondary=error,
        logo_accent=warning,
        scrollbar_track=surface,
        scrollbar_thumb=foreground,
        footer=surface,
        header=surface,
    )
    return replace(
        theme,
        contrast=ContrastClass.MAXIMUM,
        palette=palette,
        style=replace(
            theme.style,
            border_style="double",
            divider_style="solid",
            selection_style="reverse",
            bold_text=True,
            accent_intensity="vivid",
        ),
        provenance=replace(
            theme.provenance,
            recipe=f"{theme.provenance.recipe}@high-contrast",
        ),
    )


def to_textual_theme(
    theme: ThemeDefinition,
    *,
    name: str | None = None,
) -> TextualTheme:
    """Translate one semantic definition into a complete Textual theme.

    The catalog remains framework-neutral; this narrow bridge keeps the runtime,
    preview UI, and tests from interpreting semantic colors differently. Domain
    tokens are exported with a ``gucmail-`` prefix and Textual's standard focus,
    selection, link, scrollbar, Markdown, and footer variables are overridden.
    """

    from textual.theme import Theme as TextualTheme

    valid = require_valid_theme(theme)
    palette = valid.palette
    textual_name = name or f"gucmail-{valid.id.replace('.', '-').replace('_', '-')}"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", textual_name):
        raise ThemeValidationError("Textual theme name is malformed")

    semantic_variables = {
        field_name.replace("_", "-"): color
        for field_name, color in palette.colors()
        if "[" not in field_name
    }
    semantic_variables.update(
        {
            f"calendar-event-{index}": color
            for index, color in enumerate(palette.calendar_event_colors)
        }
    )
    variables = {
        **{f"gucmail-{key}": value for key, value in semantic_variables.items()},
        "text": palette.primary_text,
        "button-color-foreground": "auto 100%",
        "text-muted": palette.muted_text,
        "text-disabled": palette.disabled_text,
        "foreground-muted": palette.secondary_text,
        "foreground-disabled": palette.disabled_text,
        "border": palette.border,
        "border-blurred": palette.divider,
        "surface-active": palette.active,
        "block-cursor-background": palette.selection_background,
        "block-cursor-foreground": palette.selection_foreground,
        "block-cursor-blurred-background": palette.hover,
        "block-cursor-blurred-foreground": palette.primary_text,
        "block-hover-background": palette.hover,
        "scrollbar": palette.scrollbar_thumb,
        "scrollbar-hover": palette.primary_accent,
        "scrollbar-active": palette.secondary_accent,
        "scrollbar-background": palette.scrollbar_track,
        "scrollbar-background-hover": palette.scrollbar_track,
        "scrollbar-background-active": palette.scrollbar_track,
        "link-color": palette.link,
        "link-color-hover": palette.secondary_accent,
        "footer-background": palette.footer,
        "footer-foreground": palette.primary_text,
        "footer-key-foreground": palette.logo_accent,
        "footer-key-background": palette.footer,
        "footer-description-background": palette.footer,
        "input-selection-background": palette.selection_background,
        "markdown-h1-color": palette.primary_accent,
        "markdown-h2-color": palette.secondary_accent,
        "markdown-h3-color": palette.tertiary_accent,
        "markdown-h4-color": palette.primary_text,
        "markdown-h5-color": palette.primary_text,
        "markdown-h6-color": palette.muted_text,
    }
    return TextualTheme(
        name=textual_name,
        primary=palette.primary_accent,
        secondary=palette.secondary_accent,
        warning=palette.warning,
        error=palette.error,
        success=palette.success,
        accent=palette.tertiary_accent,
        foreground=palette.primary_text,
        background=palette.app_background,
        # Textual's standard ``$surface`` token is the safe fallback used by
        # independently mounted modal screens throughout the application.
        # Elevated non-modal surfaces use the exported semantic token.
        surface=palette.modal_background,
        panel=palette.secondary_background,
        boost=palette.hover,
        dark=valid.mode is ThemeMode.DARK,
        variables=variables,
    )


def detect_color_capability(environment: Mapping[str, str] | None = None) -> ColorCapability:
    """Conservatively detect terminal colour support without writing escape codes."""

    environ = environment if environment is not None else os.environ
    if environ.get("NO_COLOR") is not None or environ.get("TERM", "").casefold() == "dumb":
        return ColorCapability.MONOCHROME
    color_term = environ.get("COLORTERM", "").casefold()
    if color_term in {"truecolor", "24bit"}:
        return ColorCapability.TRUECOLOR
    term = environ.get("TERM", "").casefold()
    if "256color" in term:
        return ColorCapability.COLORS_256
    if term or os.name == "nt":
        return ColorCapability.COLORS_16
    return ColorCapability.MONOCHROME


def _payload_for_theme(theme: ThemeDefinition) -> dict[str, object]:
    return {
        "schema_version": theme.schema_version,
        "id": theme.id,
        "name": theme.name,
        "categories": list(theme.categories),
        "mode": theme.mode.value,
        "contrast": theme.contrast.value,
        "palette": {
            field.name: list(value)
            if isinstance(value := getattr(theme.palette, field.name), tuple)
            else value
            for field in fields(theme.palette)
        },
        "style": asdict(theme.style),
        "provenance": asdict(theme.provenance),
        "search_tags": list(theme.search_tags),
        "color_blind_friendly": theme.color_blind_friendly,
    }


def export_theme(theme: ThemeDefinition) -> str:
    """Export a custom theme as deterministic, human-readable JSON."""

    if not theme.id.startswith("custom."):
        raise ThemeValidationError("Duplicate a built-in before exporting it as a custom theme")
    return json.dumps(_payload_for_theme(theme), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _string_tuple(value: object, *, field: str, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ThemeValidationError(f"{field} must be a bounded list")
    result = tuple(_safe_text(item, field=field, maximum=80) for item in value)
    return result


def import_theme(payload: str) -> ThemeDefinition:
    """Parse and fully validate a custom theme without executing input."""

    if len(payload.encode("utf-8", errors="replace")) > 128 * 1024:
        raise ThemeValidationError("Theme file exceeds 128 KiB")
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ThemeValidationError("Theme file is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ThemeValidationError("Theme file must contain one object")
    if raw.get("schema_version") != THEME_SCHEMA_VERSION:
        raise ThemeValidationError("Unsupported theme schema version")
    theme_id = _safe_text(raw.get("id"), field="id", maximum=104)
    if not theme_id.startswith("custom."):
        raise ThemeValidationError("Imported theme IDs must start with custom.")
    name = _safe_text(raw.get("name"), field="name")
    categories = _string_tuple(raw.get("categories"), field="categories")
    if "custom" not in categories:
        categories = tuple(dict.fromkeys((*categories, "custom")))
    raw_mode = raw.get("mode")
    raw_contrast = raw.get("contrast")
    try:
        if not isinstance(raw_mode, str) or not isinstance(raw_contrast, str):
            raise ValueError
        mode = ThemeMode(raw_mode)
        contrast = ContrastClass(raw_contrast)
    except (TypeError, ValueError) as exc:
        raise ThemeValidationError("Invalid mode or contrast classification") from exc
    palette_raw = raw.get("palette")
    if not isinstance(palette_raw, dict):
        raise ThemeValidationError("palette must be an object")
    expected_palette = {field.name for field in fields(ThemePalette)}
    if set(palette_raw) != expected_palette:
        missing = sorted(expected_palette - set(palette_raw))
        unknown = sorted(set(palette_raw) - expected_palette)
        raise ThemeValidationError(f"Palette fields differ; missing={missing}, unknown={unknown}")
    palette_values: dict[str, object] = {}
    for field in fields(ThemePalette):
        value = palette_raw[field.name]
        if field.name == "calendar_event_colors":
            colors = _string_tuple(value, field=field.name, maximum=24)
            palette_values[field.name] = colors
        else:
            if not isinstance(value, str):
                raise ThemeValidationError(f"{field.name} must be a colour")
            palette_values[field.name] = value
    style_raw = raw.get("style")
    if not isinstance(style_raw, dict) or set(style_raw) != {
        field.name for field in fields(ThemeStyle)
    }:
        raise ThemeValidationError("Style fields are incomplete or unknown")
    provenance_raw = raw.get("provenance")
    parent_id: str | None = None
    if isinstance(provenance_raw, dict) and provenance_raw.get("parent_theme_id") is not None:
        parent_id = _safe_text(
            provenance_raw.get("parent_theme_id"), field="parent_theme_id", maximum=104
        )
    try:
        theme = ThemeDefinition(
            id=theme_id,
            name=name,
            categories=categories,
            mode=mode,
            contrast=contrast,
            palette=ThemePalette(**palette_values),  # type: ignore[arg-type]
            style=ThemeStyle(**style_raw),
            provenance=ThemeProvenance(
                source="imported",
                family="custom",
                recipe="imported-v1",
                parent_theme_id=parent_id,
            ),
            search_tags=_string_tuple(raw.get("search_tags", []), field="search_tags"),
            color_blind_friendly=bool(raw.get("color_blind_friendly", False)),
        )
    except TypeError as exc:
        raise ThemeValidationError("Theme fields have invalid types") from exc
    return require_valid_theme(theme)


class ThemeRegistry:
    """Searchable built-in/custom catalog with safe custom-theme operations."""

    def __init__(self, custom_themes: Iterable[ThemeDefinition] = ()) -> None:
        self._builtins = {theme.id: theme for theme in builtin_themes()}
        self._custom: dict[str, ThemeDefinition] = {}
        for theme in custom_themes:
            self.add_custom(theme)

    @property
    def builtins(self) -> tuple[ThemeDefinition, ...]:
        return tuple(self._builtins.values())

    @property
    def custom(self) -> tuple[ThemeDefinition, ...]:
        return tuple(self._custom.values())

    @property
    def themes(self) -> tuple[ThemeDefinition, ...]:
        return (*self.builtins, *self.custom)

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({category for theme in self.themes for category in theme.categories}))

    def get(self, theme_id: str) -> ThemeDefinition:
        try:
            return self._builtins.get(theme_id) or self._custom[theme_id]
        except KeyError as exc:
            raise ThemeValidationError(f"Unknown theme: {theme_id}") from exc

    def search(
        self,
        query: str = "",
        *,
        category: str | None = None,
        mode: ThemeMode | None = None,
        color_blind_friendly: bool = False,
    ) -> tuple[ThemeDefinition, ...]:
        terms = re.findall(r"[a-z0-9]+", query.casefold())
        category_key = _slug(category) if category else None

        def score(theme: ThemeDefinition) -> tuple[int, str]:
            haystack = " ".join(
                (
                    theme.name.casefold(),
                    *theme.categories,
                    *theme.search_tags,
                    theme.provenance.family,
                )
            )
            exact = sum(5 for term in terms if term in theme.name.casefold())
            broad = sum(1 for term in terms if term in haystack)
            return (-(exact + broad), theme.name.casefold())

        matches = [
            theme
            for theme in self.themes
            if (
                not terms
                or all(
                    term in " ".join((theme.name.casefold(), *theme.categories, *theme.search_tags))
                    for term in terms
                )
            )
            and (category_key is None or category_key in theme.categories)
            and (mode is None or theme.mode is mode)
            and (not color_blind_friendly or theme.color_blind_friendly)
        ]
        return tuple(sorted(matches, key=score))

    def next(self, theme_id: str, *, offset: int = 1) -> ThemeDefinition:
        themes = self.themes
        if not themes:
            raise ThemeValidationError("Theme catalog is empty")
        ids = [theme.id for theme in themes]
        try:
            index = ids.index(theme_id)
        except ValueError as exc:
            raise ThemeValidationError(f"Unknown theme: {theme_id}") from exc
        return themes[(index + offset) % len(themes)]

    def random(self, source: random.Random | None = None) -> ThemeDefinition:
        chooser = source or random.SystemRandom()
        return chooser.choice(self.themes)

    def duplicate(
        self, source_id: str, *, name: str, custom_id: str | None = None
    ) -> ThemeDefinition:
        source = self.get(source_id)
        safe_name = _safe_text(name, field="name")
        base_id = custom_id or f"custom.{_slug(safe_name)}"
        if not base_id.startswith("custom."):
            base_id = f"custom.{_slug(base_id)}"
        candidate_id = base_id
        suffix = 2
        while candidate_id in self._custom or candidate_id in self._builtins:
            candidate_id = f"{base_id}-{suffix}"
            suffix += 1
        theme = replace(
            source,
            id=candidate_id,
            name=safe_name,
            categories=tuple(sorted({*source.categories, "custom"} - {"built-in"})),
            provenance=ThemeProvenance(
                source="custom",
                family="custom",
                recipe="duplicate-v1",
                parent_theme_id=source.id,
            ),
        )
        self.add_custom(theme)
        return theme

    def add_custom(self, theme: ThemeDefinition) -> ThemeDefinition:
        if not theme.id.startswith("custom."):
            raise ThemeValidationError("Only custom.* themes can be added")
        valid = require_valid_theme(theme)
        if valid.id in self._builtins:
            raise ThemeValidationError("A custom theme cannot replace a built-in")
        self._custom[valid.id] = valid
        return valid

    def edit_custom(
        self,
        theme_id: str,
        *,
        palette_overrides: Mapping[str, str | Sequence[str]] | None = None,
        style_overrides: Mapping[str, object] | None = None,
    ) -> ThemeDefinition:
        theme = self._require_custom(theme_id)
        palette_data: dict[str, object] = {
            field.name: getattr(theme.palette, field.name) for field in fields(theme.palette)
        }
        if palette_overrides:
            unknown = set(palette_overrides) - set(palette_data)
            if unknown:
                raise ThemeValidationError(f"Unknown palette fields: {sorted(unknown)}")
            for key, value in palette_overrides.items():
                palette_data[key] = (
                    tuple(value)
                    if key == "calendar_event_colors" and not isinstance(value, str)
                    else value
                )
        style_data = asdict(theme.style)
        if style_overrides:
            unknown_style = set(style_overrides) - set(style_data)
            if unknown_style:
                raise ThemeValidationError(f"Unknown style fields: {sorted(unknown_style)}")
            style_data.update(style_overrides)
        try:
            updated = replace(
                theme,
                palette=ThemePalette(**palette_data),  # type: ignore[arg-type]
                style=ThemeStyle(**style_data),
            )
        except TypeError as exc:
            raise ThemeValidationError("Invalid custom theme override") from exc
        self._custom[theme_id] = require_valid_theme(updated)
        return updated

    def rename_custom(self, theme_id: str, name: str) -> ThemeDefinition:
        updated = replace(self._require_custom(theme_id), name=_safe_text(name, field="name"))
        self._custom[theme_id] = require_valid_theme(updated)
        return updated

    def delete_custom(self, theme_id: str) -> ThemeDefinition:
        theme = self._require_custom(theme_id)
        del self._custom[theme_id]
        return theme

    def reset_custom(self) -> None:
        self._custom.clear()

    def import_custom(self, payload: str) -> ThemeDefinition:
        theme = import_theme(payload)
        if theme.id in self._custom:
            raise ThemeValidationError(f"Custom theme already exists: {theme.id}")
        return self.add_custom(theme)

    def _require_custom(self, theme_id: str) -> ThemeDefinition:
        try:
            return self._custom[theme_id]
        except KeyError as exc:
            raise ThemeValidationError(f"Custom theme not found: {theme_id}") from exc


class ThemeSession:
    """Staged live-preview model with exact cancellation and persistence hooks."""

    def __init__(
        self,
        registry: ThemeRegistry,
        preferences: ThemePreferences | None = None,
        *,
        persist: Callable[[ThemePreferences], None] | None = None,
        recent_limit: int = 12,
    ) -> None:
        self.registry = registry
        self.preferences = preferences or ThemePreferences()
        self._persist = persist
        self._recent_limit = max(1, min(recent_limit, 50))
        self._preview_id: str | None = None
        self.registry.get(self.preferences.selected_theme_id)

    @property
    def applied(self) -> ThemeDefinition:
        return self.registry.get(self.preferences.selected_theme_id)

    @property
    def current(self) -> ThemeDefinition:
        return self.registry.get(self._preview_id) if self._preview_id else self.applied

    @property
    def is_previewing(self) -> bool:
        return self._preview_id is not None

    def preview(self, theme_id: str) -> ThemeDefinition:
        theme = self.registry.get(theme_id)
        self._preview_id = theme.id
        return theme

    def preview_random(self, source: random.Random | None = None) -> ThemeDefinition:
        return self.preview(self.registry.random(source).id)

    def cancel(self) -> ThemeDefinition:
        self._preview_id = None
        return self.applied

    def apply(self, theme_id: str | None = None) -> ThemeDefinition:
        selected = self.registry.get(theme_id or self._preview_id or self.applied.id)
        recent = tuple(dict.fromkeys((selected.id, *self.preferences.recently_used)))[
            : self._recent_limit
        ]
        previous_preferences = self.preferences
        previous_preview_id = self._preview_id
        self.preferences = replace(
            previous_preferences,
            selected_theme_id=selected.id,
            recently_used=recent,
        )
        try:
            self._save()
        except Exception:
            self.preferences = previous_preferences
            self._preview_id = previous_preview_id
            raise
        self._preview_id = None
        return selected

    def cycle(self, offset: int = 1, *, apply: bool = False) -> ThemeDefinition:
        selected = self.registry.next(self.current.id, offset=offset)
        self.preview(selected.id)
        return self.apply() if apply else selected

    def toggle_favorite(self, theme_id: str | None = None) -> bool:
        selected = self.registry.get(theme_id or self.current.id)
        favorites = list(self.preferences.favorites)
        if selected.id in favorites:
            favorites.remove(selected.id)
            state = False
        else:
            favorites.insert(0, selected.id)
            state = True
        previous_preferences = self.preferences
        self.preferences = replace(previous_preferences, favorites=tuple(favorites[:100]))
        try:
            self._save()
        except Exception:
            self.preferences = previous_preferences
            raise
        return state

    def favorite_themes(self) -> tuple[ThemeDefinition, ...]:
        return tuple(
            self.registry.get(theme_id)
            for theme_id in self.preferences.favorites
            if theme_id in {theme.id for theme in self.registry.themes}
        )

    def recent_themes(self) -> tuple[ThemeDefinition, ...]:
        existing = {theme.id for theme in self.registry.themes}
        return tuple(
            self.registry.get(theme_id)
            for theme_id in self.preferences.recently_used
            if theme_id in existing
        )

    def _save(self) -> None:
        if self._persist is not None:
            self._persist(self.preferences)


def resolve_theme_request(
    registry: ThemeRegistry,
    request: ThemeRequest,
    *,
    when: datetime | None = None,
    system_dark: Callable[[], bool | None] | None = None,
    source: random.Random | None = None,
) -> ThemeDefinition:
    """Resolve explicit, automatic light/dark/time, and random theme policies."""

    if not 0 <= request.day_starts_at <= 23 or not 0 <= request.night_starts_at <= 23:
        raise ThemeValidationError("Automatic theme hours must be between 0 and 23")
    if request.policy is ThemePolicy.RANDOM:
        return registry.random(source)
    if request.policy is ThemePolicy.EXPLICIT:
        return registry.get(request.selected_theme_id)
    if request.policy is ThemePolicy.DARK:
        return registry.get(request.dark_theme_id)
    if request.policy is ThemePolicy.LIGHT:
        return registry.get(request.light_theme_id)
    if request.policy is ThemePolicy.SYSTEM:
        detected = system_dark() if system_dark else None
        if detected is not None:
            return registry.get(request.dark_theme_id if detected else request.light_theme_id)
    current = when or datetime.now().astimezone()
    is_day = request.day_starts_at <= current.hour < request.night_starts_at
    return registry.get(request.light_theme_id if is_day else request.dark_theme_id)
