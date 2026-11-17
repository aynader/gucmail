"""Terminal-safe, theme-aware branding for GUC Mail CLI.

All presets are code-native and available offline.  The renderer returns semantic
spans rather than embedding ANSI escapes, allowing Textual/Rich or a plain terminal
to decide how colour is emitted.  The logo presets include both a geometric trace
and an alpha-mask sample generated from ``guclogo.png`` without requiring that
raster at run time.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import lru_cache
from typing import Final

from pyfiglet import Figlet, FigletFont

from gucmail.tui.themes import (
    ColorCapability,
    ThemeDefinition,
    contrast_ratio,
)

SLIM_TITLE: Final = "GUC Mail CLI"  # the tricolour GUC logo mark, not a label
GERMAN_BLACK: Final = "#000000"
GERMAN_RED: Final = "#dd0000"
GERMAN_GOLD: Final = "#ffcc00"
GUC_LOGO_SOURCE_SHA256: Final = "3a41e4c8eeefdbf3a8185cbf13415007ab32deeaf941ab8e8d7d932ee8eb075f"
_MAX_ART_WIDTH: Final = 200
_MAX_ART_HEIGHT: Final = 32


class BrandingValidationError(ValueError):
    """Raised for unsafe or malformed branding preferences."""


class BrandRole(StrEnum):
    PRIMARY = "logo_primary"
    SECONDARY = "logo_secondary"
    ACCENT = "logo_accent"
    TEXT = "primary_text"


class BrandSize(StrEnum):
    AUTO = "auto"
    FULL = "full"
    COMPACT = "compact"
    MINIMAL = "minimal"


class BrandAlignment(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class BrandColorBehavior(StrEnum):
    THEME = "theme"
    GERMAN_FLAG = "german-flag"
    MONOCHROME = "monochrome"


@dataclass(frozen=True, slots=True)
class BrandColor:
    foreground: str | None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class BrandColorSet:
    primary: BrandColor
    secondary: BrandColor
    accent: BrandColor
    text: BrandColor

    def for_role(self, role: BrandRole) -> BrandColor:
        return {
            BrandRole.PRIMARY: self.primary,
            BrandRole.SECONDARY: self.secondary,
            BrandRole.ACCENT: self.accent,
            BrandRole.TEXT: self.text,
        }[role]


@dataclass(frozen=True, slots=True)
class BrandSpan:
    text: str
    role: BrandRole
    foreground: str | None
    background: str | None = None


@dataclass(frozen=True, slots=True)
class BrandLine:
    spans: tuple[BrandSpan, ...]

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


@dataclass(frozen=True, slots=True)
class RenderedBranding:
    preset_id: str
    variant: BrandSize
    lines: tuple[BrandLine, ...]
    color_behavior: BrandColorBehavior
    accessible_color_adjustment: bool = False
    font_backend: str | None = None
    font_name: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def width(self) -> int:
        return max((len(line.text) for line in self.lines), default=0)

    @property
    def height(self) -> int:
        return len(self.lines)


@dataclass(frozen=True, slots=True)
class BrandingPreset:
    id: str
    name: str
    category: str
    full_lines: tuple[str, ...]
    compact_lines: tuple[str, ...]
    minimal_text: str
    full_roles: tuple[BrandRole, ...]
    provenance: str
    description: str
    font_backend: str | None = None
    font_name: str | None = None
    fallback_font: str | None = None
    fallback_reason: str | None = None

    def lines_for(self, size: BrandSize) -> tuple[str, ...]:
        if size is BrandSize.MINIMAL:
            return (self.minimal_text,)
        if size is BrandSize.COMPACT:
            return self.compact_lines
        if self.font_backend == "pyfiglet" and self.font_name is not None:
            return _render_figlet(self.font_name, self.fallback_font or "standard")
        return self.full_lines


@dataclass(frozen=True, slots=True)
class BrandingPreferences:
    preset_id: str = "builtin.guc-mail-minimal"
    visible: bool = True
    size: BrandSize = BrandSize.AUTO
    color_behavior: BrandColorBehavior = BrandColorBehavior.THEME
    alignment: BrandAlignment = BrandAlignment.LEFT
    custom_ascii: str = ""
    target_width: int | None = None
    target_height: int | None = None


def _preset(
    identifier: str,
    name: str,
    category: str,
    full: tuple[str, ...],
    compact: tuple[str, ...],
    minimal: str,
    roles: tuple[BrandRole, ...],
    provenance: str,
    description: str,
    *,
    font_backend: str | None = None,
    font_name: str | None = None,
    fallback_font: str | None = None,
    fallback_reason: str | None = None,
) -> BrandingPreset:
    if not identifier.startswith("builtin."):
        raise BrandingValidationError("Built-in branding IDs must start with builtin.")
    if not full or not compact or not minimal:
        raise BrandingValidationError("Every visible preset needs full, compact, and minimal forms")
    if len(roles) != len(full):
        raise BrandingValidationError("Every full-size line needs a semantic colour role")
    for line in (*full, *compact, minimal):
        if any(ord(character) < 32 for character in line):
            raise BrandingValidationError("Built-in branding must be terminal-safe")
    return BrandingPreset(
        id=identifier,
        name=name,
        category=category,
        full_lines=full,
        compact_lines=compact,
        minimal_text=minimal,
        full_roles=roles,
        provenance=provenance,
        description=description,
        font_backend=font_backend,
        font_name=font_name,
        fallback_font=fallback_font,
        fallback_reason=fallback_reason,
    )


_HANDCRAFTED_PRESETS: Final[tuple[BrandingPreset, ...]] = (
    _preset(
        "builtin.guc-original-mark",
        "GUC Logo · Dotted mark",
        "logo",
        (SLIM_TITLE,),
        (SLIM_TITLE,),
        SLIM_TITLE,
        (BrandRole.TEXT,),
        "color sample of the supplied GUC logo",
        "A color-sampled dotted approximation of the GUC mark; text fallback on small terminals.",
    ),
    _preset(
        "builtin.guc-logo-image",
        "GUC Logo · Image Trace",
        "logo",
        (
            "           .-=-",
            "        .=#@@@@*",
            "      :*@#**@@@@",
            "     +#+.   +@@+",
            "   -#+.    :@@+*.",
            "  :*:     -@@- *:",
            " +##+-.  +@*.  #:",
            ":@@@@@@#+++.   #:",
            ".@@@+:-+@+#-   #:",
            " +@@     .#+   @:",
            "  #@=     *#  :@.",
            "  .#@-  -.+@: +@",
            "   .#@= +@=@#+@*",
            "     =@##@=@@@@=",
            "      .+#@*+@@@.",
            "         :-.+*:",
        ),
        ("   .+@@@", " +@+  @", "@@@*. @", "  *@@@@"),
        "GUC",
        (
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
        ),
        "guclogo.png 18x18 alpha-density conversion",
        "A reproducible image-to-ASCII trace of the repository's GUC ribbon mark.",
    ),
    _preset(
        "builtin.guc-logo-image-sampled",
        "GUC Logo Image Sample",
        "logo",
        (
            "            +++###@@@@@@@@#",
            "       ++####++++  +#@@@@@#",
            "   ++##++        +#@@@#+ +@#",
            "+@@@@@@@@####+###@@+     +@#",
            " #@@@@#   +++++++@@+     #@#",
            "   +#@@#+        #@@+   +@@+",
            "      +##@##+#@@@+@@@@@@@@+",
            "           +++####++@@@@#+",
        ),
        (
            "      ++++++#@@@@+",
            "  ++#+++  ++###+##",
            "+@@@#+++++#@+   #@",
            "  +##++  ++@@+++@+",
            "     ++++@@+#@@@+",
        ),
        "GUC",
        (
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
        ),
        "guclogo.png alpha-mask sample at 28x8; source SHA-256 " + GUC_LOGO_SOURCE_SHA256,
        "A direct offline image-to-ASCII sample of the repository GUC ribbon mark.",
    ),
    _preset(
        "builtin.guc-ribbon",
        "GUC Ribbon Logo",
        "logo",
        (
            "           ///////////",
            "       /////       //",
            "    ////           /",
            "  ///     /////////",
            " ###    ###",
            "#####       ###",
            "  ###########",
        ),
        ("   ///////", " ///   //", "##  ###", " ######"),
        "GUC",
        (
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
        ),
        "guclogo.png geometric ribbon trace",
        "Red upper and gold lower ribbons inspired by the repository GUC mark.",
    ),
    _preset(
        "builtin.guc-ribbon-outline",
        "GUC Ribbon Outline",
        "logo",
        (
            "          ___________",
            "      ___/          /",
            "   __/             /",
            "  /      _________/",
            " /     __/",
            "/____/      ___",
            "    \\_______/",
        ),
        ("  ______", " /   _/", "/_ /__", "  \\__/"),
        "[GUC]",
        (
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
            BrandRole.ACCENT,
        ),
        "guclogo.png outline interpretation",
        "An airy outline of the GUC ribbon for lower-density themes.",
    ),
    _preset(
        "builtin.guc-block",
        "GUC Block",
        "wordmark",
        (
            "  GGGG   U   U   CCCC",
            " G       U   U  C",
            " G  GG   U   U  C",
            " G   G   U   U  C",
            "  GGGG    UUU    CCCC",
        ),
        ("GGG U U CCC", "G G U U C", "GGG UUU CCC"),
        "GUC",
        (
            BrandRole.PRIMARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
        ),
        "code-native block alphabet",
        "A robust ASCII-only GUC wordmark.",
    ),
    _preset(
        "builtin.guc-slant",
        "GUC Slant",
        "wordmark",
        (
            "   ____  __  __  ______",
            "  / __/ / / / / / ____/",
            " / /_  / / / / / /",
            "/___/  \\____/ /____/",
        ),
        (" /__/ / / /__", "/__/ /_/ /__"),
        "/G/U/C/",
        (BrandRole.PRIMARY, BrandRole.SECONDARY, BrandRole.ACCENT, BrandRole.PRIMARY),
        "code-native slanted alphabet",
        "A light, technical slanted GUC wordmark.",
    ),
    _preset(
        "builtin.guc-circuit",
        "GUC Circuit",
        "wordmark",
        (
            "+--G--+     +--U--+     +--C--+",
            "|  +--+     |     |     |",
            "|  |  |     |     |     |",
            "+--+  +-----+-----+     +-----+",
        ),
        ("+-G-+ +-U-+ +-C-+", "+---+ +---+ +---+"),
        "G-U-C",
        (BrandRole.PRIMARY, BrandRole.SECONDARY, BrandRole.ACCENT, BrandRole.PRIMARY),
        "code-native circuit motif",
        "An engineering-oriented circuit-line wordmark.",
    ),
    _preset(
        "builtin.guc-mail-standard",
        "GUC Mail Standard",
        "banner",
        (
            "  GGG   U   U   CCC      M   M   AAA   III  L",
            " G      U   U  C         MM MM  A   A   I   L",
            " G GG   U   U  C         M M M  AAAAA   I   L",
            " G  G   U   U  C         M   M  A   A   I   L",
            "  GGG    UUU    CCC      M   M  A   A  III  LLLLL",
        ),
        ("GUC MAIL", "--------"),
        SLIM_TITLE,
        (
            BrandRole.PRIMARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
        ),
        "code-native standard banner",
        "A full ASCII GUC Mail banner with a stable compact fallback.",
    ),
    _preset(
        "builtin.guc-mail-shadow",
        "GUC Mail Shadow",
        "banner",
        (
            " .----. .-..-. .----.   .-.  .-. .---.  .-..-.",
            ": .--' : `' : : .--'   : :\\/: : : .; : : :: :",
            ": : _  : .. : : :      : ::  : :   .' : :: :",
            ": :_; : : :; : : :__   : :  : : : :_  : :; :",
            " `----' `----'  `----'  `-'  `-' `.__.' `----'",
        ),
        (".GUC MAIL.", "`--------'"),
        ":: GUC Mail CLI ::",
        (
            BrandRole.PRIMARY,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
            BrandRole.SECONDARY,
            BrandRole.ACCENT,
        ),
        "code-native shadow banner",
        "A rounded shadow banner for expressive themes.",
    ),
    _preset(
        "builtin.guc-mail-boxed",
        "GUC Mail Boxed",
        "banner",
        (
            "+====================================+",
            "|   G U C   M A I L   C L I          |",
            "|   Exchange, focused in your terminal |",
            "+====================================+",
        ),
        ("+-- GUC MAIL CLI --+",),
        "[GUC Mail CLI]",
        (BrandRole.ACCENT, BrandRole.PRIMARY, BrandRole.TEXT, BrandRole.ACCENT),
        "code-native boxed banner",
        "A framed banner with no dynamic or remote content.",
    ),
    _preset(
        "builtin.guc-mail-campus",
        "GUC Mail Campus",
        "banner",
        (
            "       /\\          G U C   M A I L",
            "  ____/  \\____      ================",
            " |  _  __  _  |     Command your inbox",
            " |_| ||  || |_|",
        ),
        ("/\\  GUC MAIL", "||  CLI"),
        "GUC Mail",
        (BrandRole.ACCENT, BrandRole.SECONDARY, BrandRole.PRIMARY, BrandRole.ACCENT),
        "code-native campus silhouette",
        "A restrained university/campus banner.",
    ),
    _preset(
        "builtin.guc-mail-terminal",
        "GUC Mail Terminal",
        "banner",
        (
            "$ gucmail tui",
            "> GUC Mail CLI",
            "> mailbox ready_",
        ),
        ("> GUC Mail CLI",),
        "> GUC Mail CLI",
        (BrandRole.ACCENT, BrandRole.PRIMARY, BrandRole.SECONDARY),
        "code-native terminal prompt",
        "A compact terminal prompt suited to retro themes.",
    ),
    _preset(
        "builtin.guc-mail-minimal",
        "GUC Mail Minimal",
        "minimal",
        (SLIM_TITLE,),
        (SLIM_TITLE,),
        SLIM_TITLE,
        (BrandRole.TEXT,),
        "canonical product title",
        "The exact product title, without decorative output.",
    ),
    _preset(
        "builtin.guc-tricolor",
        "GUC Tricolor",
        "minimal",
        ("G U C", "Mail CLI"),
        ("G U C | Mail CLI",),
        SLIM_TITLE,
        (BrandRole.PRIMARY, BrandRole.TEXT),
        "German tricolor semantic wordmark",
        "A small G/U/C mark designed for black-red-gold semantic spans.",
    ),
)


_PIXEL_GLYPHS: Final[dict[str, tuple[str, ...]]] = {
    "G": ("11111", "10000", "10000", "10111", "10001", "10001", "11111"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "11111"),
    "C": ("11111", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    " ": ("000", "000", "000", "000", "000", "000", "000"),
}


def _pixel_wordmark(text: str, fill: str, effect: str) -> tuple[str, ...]:
    """Render a code-native bitmap wordmark with a genuine visual treatment."""

    matrix = tuple(
        " ".join(
            "".join(fill if cell == "1" else " " for cell in _PIXEL_GLYPHS[character][row])
            for character in text
        ).rstrip()
        for row in range(7)
    )
    if effect == "plain":
        return matrix
    if effect == "slant":
        return tuple(" " * (6 - row) + line for row, line in enumerate(matrix))
    if effect == "shadow":
        width = max(len(line) for line in matrix) + 2
        canvas = [list(line.ljust(width)) for line in matrix]
        canvas.append(list(" " * width))
        for row, line in enumerate(matrix):
            for column, character in enumerate(line):
                if character != " " and canvas[row + 1][column + 1] == " ":
                    canvas[row + 1][column + 1] = "."
        return tuple("".join(line).rstrip() for line in canvas)
    if effect == "double":
        doubled = tuple("".join(character * 2 for character in line) for line in matrix)
        return doubled
    raise BrandingValidationError(f"Unknown generated branding effect: {effect}")


def _generated_presets() -> tuple[BrandingPreset, ...]:
    """Build a broad catalog of visually distinct, dependency-free banners."""

    fills = (
        ("hash", "#", "Hash"),
        ("star", "*", "Star"),
        ("plus", "+", "Plus"),
        ("at", "@", "At"),
        ("percent", "%", "Percent"),
        ("equals", "=", "Equal"),
        ("ampersand", "&", "Ampersand"),
        ("letter", "G", "Letterpress"),
    )
    effects = (
        ("plain", "Block"),
        ("slant", "Diagonal"),
        ("shadow", "Shadow"),
        ("double", "Wide"),
    )
    scopes = (
        ("guc", "GUC", "wordmark", "GUC"),
        ("mail", "GUC MAIL", "banner", SLIM_TITLE),
    )
    generated: list[BrandingPreset] = []
    for fill_id, fill, fill_name in fills:
        for effect_id, effect_name in effects:
            for scope_id, text, category, minimal in scopes:
                lines = _pixel_wordmark(text, fill, effect_id)
                roles = tuple(
                    (BrandRole.PRIMARY, BrandRole.SECONDARY, BrandRole.ACCENT)[index % 3]
                    for index in range(len(lines))
                )
                generated.append(
                    _preset(
                        f"builtin.pixel-{scope_id}-{fill_id}-{effect_id}",
                        f"{text.title()} {fill_name} {effect_name}",
                        f"generated-{category}",
                        lines,
                        (f"{text} · {fill_name} {effect_name}",),
                        minimal,
                        roles,
                        "code-native 5x7 pixel font generator",
                        (
                            f"A dependency-free {fill_name.lower()} {effect_name.lower()} "
                            f"{category} with adaptive compact and minimal forms."
                        ),
                    )
                )
    return tuple(generated)


_FEATURED_FONT_NAMES: Final[tuple[str, ...]] = (
    "3D Diagonal",
    "Alpha",
    "Acrobatic",
    "Avatar",
    "Babyface Lame",
    "Babyface Leet",
    "Big Money-ne",
    "Big Money-nw",
    "Big Money-se",
    "Big Money-sw",
    "Big",
    "Blocks",
    "BlurVision ASCII",
    "Bulbhead",
    "Cards",
    "Chiseled",
    "Crawford2",
    "Crazy",
    "Dancing Font",
    "DiamFont",
    "Doh",
    "Doom",
    "Efti Wall",
    "Epic",
    "Fire Font-k",
    "Fire Font-s",
    "Flower Power",
    "Fun Face",
    "Fun Faces",
    "Ghost",
    "Graceful",
    "Graffiti",
    "Impossible",
    "Isometric1",
    "Isometric2",
    "Isometric3",
    "Isometric4",
    "JS Bracket Letters",
    "Lil Devil",
    "Merlin1",
    "miniwi",
    "Modular",
    "Ogre",
    "Patorjk's Cheese",
    "Patorjk-HeX",
    "Rectangles",
    "RubiFont",
    "Shaded Blocky",
    "Slant",
    "Slant Relief",
    "Small",
    "Small Slant",
    "Small Isometric1",
    "Soft",
    "Standard",
    "Star Wars",
    "Sub-Zero",
    "Swamp Land",
    "Sweet",
    "Terrace",
    "Tmplr",
    "Train",
    "Twisted",
    "Wavescape",
    "Wet Letter",
    "Varsity",
)
_ANSI_FONT_NAMES: Final[tuple[str, ...]] = (
    "3D-ASCII",
    "ANSI Compact",
    "ANSI Regular",
    "ANSI Shadow",
    "Bloody",
    "Calvin S",
    "Classy",
    "Coder Mini",
    "Delta Corps Priest 1",
    "Electronic",
    "Elite",
    "Stronger Than All",
    "THIS",
    "The Edge",
)
_TOILET_FONT_NAMES: Final[tuple[str, ...]] = (
    "ASCII 12",
    "ASCII 9",
    "Big ASCII 12",
    "Big ASCII 9",
    "Circle",
    "Emboss",
    "Emboss 2",
    "Future",
    "Future Smooth",
    "Future Thin",
    "Letter",
    "Mono 12",
    "Mono 9",
    "Pagga",
    "Rebel",
    "Small ASCII 12",
    "Small ASCII 9",
    "Small Block",
    "Small Braille",
    "Small Mono 12",
    "Small Mono 999",
)


def _font_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _font_label(value: str) -> str:
    return re.sub(r"[_-]+", " ", value).strip().title()


def _figlet_presets() -> tuple[BrandingPreset, ...]:
    """Expose the installed offline FIGlet corpus without rendering it at startup."""

    available = tuple(FigletFont.getFonts())  # type: ignore[no-untyped-call]
    available_by_key = {_font_key(font): font for font in available}
    requested: dict[str, tuple[str, str]] = {}
    for category, names in (
        ("featured-figlet", _FEATURED_FONT_NAMES),
        ("ansi-figlet", _ANSI_FONT_NAMES),
        ("toilet-font", _TOILET_FONT_NAMES),
    ):
        for name in names:
            requested.setdefault(_font_key(name), (name, category))

    presets: list[BrandingPreset] = []
    for font in available:
        display_name, category = requested.get(
            _font_key(font),
            (_font_label(font), "figlet-font"),
        )
        presets.append(
            _preset(
                f"builtin.figlet-{font}",
                f"GUC Mail · {display_name}",
                category,
                (SLIM_TITLE,),
                (f"GUC Mail · {display_name}",),
                SLIM_TITLE,
                (BrandRole.PRIMARY,),
                f"pyfiglet {font} offline font",
                f"The GUC Mail wordmark rendered with the {display_name} FIGlet style.",
                font_backend="pyfiglet",
                font_name=font,
                fallback_font="standard",
            )
        )

    for key, (display_name, category) in requested.items():
        if key in available_by_key:
            continue
        reason = (
            f"The requested {display_name} font is not distributed by the installed "
            "pyfiglet backend; Standard is used explicitly."
        )
        presets.append(
            _preset(
                f"builtin.figlet-fallback-{key}",
                f"GUC Mail · {display_name} (fallback)",
                category,
                (SLIM_TITLE,),
                (f"GUC Mail · {display_name} · Standard fallback",),
                SLIM_TITLE,
                (BrandRole.PRIMARY,),
                reason,
                reason,
                font_backend="pyfiglet",
                font_name="standard",
                fallback_font="standard",
                fallback_reason=reason,
            )
        )
    return tuple(presets)


@lru_cache(maxsize=1)
def _common_presets() -> dict[str, BrandingPreset]:
    """Hand-made and generated presets: enough to resolve a saved choice at startup."""

    return {preset.id: preset for preset in (*_HANDCRAFTED_PRESETS, *_generated_presets())}


@lru_cache(maxsize=1)
def _builtin_presets() -> tuple[BrandingPreset, ...]:
    """Every preset, including one per installed FIGlet font (reads ~600 font files)."""

    return (*_common_presets().values(), *_figlet_presets())


class BrandingCatalog:
    """Broad searchable catalog; custom art lives in preferences, never executable data.

    The FIGlet font presets are listed only when the whole catalog is needed, so
    resolving the saved logo at startup stays fast.
    """

    def __init__(self, presets: Iterable[BrandingPreset] | None = None) -> None:
        self._builtin = presets is None
        self._indexed: dict[str, BrandingPreset] | None = None
        if presets is not None:
            self._indexed = self._index(tuple(presets))

    @staticmethod
    def _index(presets: tuple[BrandingPreset, ...]) -> dict[str, BrandingPreset]:
        indexed = {preset.id: preset for preset in presets}
        if len(indexed) != len(presets):
            raise BrandingValidationError("Branding IDs must be unique")
        return indexed

    def _all(self) -> dict[str, BrandingPreset]:
        if self._indexed is None:
            self._indexed = self._index(_builtin_presets())
        return self._indexed

    @property
    def presets(self) -> tuple[BrandingPreset, ...]:
        return tuple(self._all().values())

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({preset.category for preset in self.presets}))

    def get(self, preset_id: str) -> BrandingPreset:
        if self._builtin and self._indexed is None and preset_id in _common_presets():
            return _common_presets()[preset_id]
        try:
            return self._all()[preset_id]
        except KeyError as exc:
            raise BrandingValidationError(f"Unknown branding preset: {preset_id}") from exc

    def search(self, query: str = "", *, category: str | None = None) -> tuple[BrandingPreset, ...]:
        terms = re.findall(r"[a-z0-9]+", query.casefold())
        result = [
            preset
            for preset in self.presets
            if (category is None or preset.category == category)
            and all(
                term
                in " ".join(
                    (
                        preset.name.casefold(),
                        preset.category.casefold(),
                        preset.description.casefold(),
                        preset.provenance.casefold(),
                    )
                )
                for term in terms
            )
        ]
        return tuple(sorted(result, key=lambda preset: preset.name.casefold()))


def sanitize_custom_ascii(value: str, *, unicode_supported: bool = True) -> str:
    """Normalize custom art while removing terminal controls and bidi overrides."""

    if not isinstance(value, str):
        raise BrandingValidationError("Custom branding must be text")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    safe_lines: list[str] = []
    for raw_line in normalized.split("\n"):
        safe_characters: list[str] = []
        for character in raw_line.replace("\t", "    "):
            codepoint = ord(character)
            category = unicodedata.category(character)
            if codepoint == 0x1B or category in {"Cc", "Cf", "Cs"}:
                continue
            if not unicode_supported and codepoint > 0x7E:
                safe_characters.append("?")
            else:
                safe_characters.append(character)
        safe_lines.append("".join(safe_characters).rstrip())
    while safe_lines and not safe_lines[-1]:
        safe_lines.pop()
    if len(safe_lines) > _MAX_ART_HEIGHT:
        raise BrandingValidationError(f"Custom branding may have at most {_MAX_ART_HEIGHT} lines")
    if any(len(line) > _MAX_ART_WIDTH for line in safe_lines):
        raise BrandingValidationError(f"Custom branding may be at most {_MAX_ART_WIDTH} columns")
    return "\n".join(safe_lines)


@lru_cache(maxsize=256)
def _render_figlet(font_name: str, fallback_font: str) -> tuple[str, ...]:
    """Render a selected offline font lazily and fail closed to Standard."""

    try:
        rendered = Figlet(font=font_name, width=_MAX_ART_WIDTH).renderText("GUC Mail")
    except Exception:
        try:
            rendered = Figlet(font=fallback_font, width=_MAX_ART_WIDTH).renderText("GUC Mail")
        except Exception:
            return (SLIM_TITLE,)
    safe = sanitize_custom_ascii(rendered)
    return tuple(safe.splitlines()) or (SLIM_TITLE,)


def resize_ascii_art(lines: tuple[str, ...], width: int, height: int) -> tuple[str, ...]:
    """Resize character art using deterministic nearest-neighbour image sampling."""

    if not lines:
        return ()
    if not 1 <= width <= _MAX_ART_WIDTH or not 1 <= height <= _MAX_ART_HEIGHT:
        raise BrandingValidationError(
            f"Branding size must be within 1..{_MAX_ART_WIDTH} by 1..{_MAX_ART_HEIGHT}"
        )
    source_width = max(len(line) for line in lines)
    if source_width == 0:
        return tuple("" for _ in range(height))
    canvas = tuple(line.ljust(source_width) for line in lines)
    resized: list[str] = []
    for output_y in range(height):
        source_y = min(len(canvas) - 1, (output_y * len(canvas)) // height)
        row = "".join(
            canvas[source_y][min(source_width - 1, (output_x * source_width) // width)]
            for output_x in range(width)
        )
        resized.append(row.rstrip())
    return tuple(resized)


def _variant_for(
    requested: BrandSize,
    *,
    terminal_width: int,
    max_height: int,
) -> BrandSize:
    if requested is not BrandSize.AUTO:
        return requested
    if terminal_width < 36 or max_height < 2:
        return BrandSize.MINIMAL
    # Keep this boundary aligned with the main TUI's ``narrow`` breakpoint.  The
    # app reserves two columns around the expanded brand, so an 80-column screen
    # supplies at most 78 columns here and correctly receives the compact form.
    if terminal_width < 80 or max_height < 5:
        return BrandSize.COMPACT
    return BrandSize.FULL


def _legible_brand_color(foreground: str, header: str) -> tuple[str, bool]:
    if contrast_ratio(foreground, header) >= 3.0:
        return foreground, False
    candidates = ("#f5f5f5", "#111111")
    replacement = max(candidates, key=lambda candidate: contrast_ratio(header, candidate))
    channels = tuple(int(foreground.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4))
    if max(channels) - min(channels) < 32:
        # Black (or another neutral) has no hue to keep: use the readable neutral.
        return replacement, True
    # Keep a chromatic brand colour recognisable (red stays red, gold stays gold)
    # by tinting it toward the readable neutral just far enough for 3:1.
    target = tuple(int(replacement.lstrip("#")[index : index + 2], 16) for index in (0, 2, 4))
    for step in range(1, 21):
        weight = step / 20
        mixed = "#" + "".join(
            f"{round(source + (goal - source) * weight):02x}"
            for source, goal in zip(channels, target, strict=True)
        )
        if contrast_ratio(mixed, header) >= 3.0:
            return mixed, True
    return replacement, True


def resolve_brand_colors(
    theme: ThemeDefinition,
    behavior: BrandColorBehavior,
    capability: ColorCapability,
) -> tuple[BrandColorSet, bool]:
    """Resolve legible brand colours on the uninterrupted header background."""

    if capability is ColorCapability.MONOCHROME or behavior is BrandColorBehavior.MONOCHROME:
        plain = BrandColor(None)
        return BrandColorSet(plain, plain, plain, plain), False
    if behavior is BrandColorBehavior.GERMAN_FLAG:
        adjusted = False
        colors: list[BrandColor] = []
        for color in (GERMAN_BLACK, GERMAN_RED, GERMAN_GOLD):
            foreground, changed = _legible_brand_color(color, theme.palette.header)
            colors.append(BrandColor(foreground))
            adjusted = adjusted or changed
        return (
            BrandColorSet(
                primary=colors[0],
                secondary=colors[1],
                accent=colors[2],
                text=BrandColor(theme.palette.primary_text),
            ),
            adjusted,
        )
    return (
        BrandColorSet(
            primary=BrandColor(theme.palette.logo_primary),
            secondary=BrandColor(theme.palette.logo_secondary),
            accent=BrandColor(theme.palette.logo_accent),
            text=BrandColor(theme.palette.primary_text),
        ),
        False,
    )


def _semantic_spans(text: str, colors: BrandColorSet) -> tuple[BrandSpan, ...]:
    """Color the canonical G/U/C prefix semantically and leave the suffix readable."""

    match = re.search(r"G\s*U\s*C", text, flags=re.IGNORECASE)
    if match is None:
        color = colors.text
        return (BrandSpan(text, BrandRole.TEXT, color.foreground, color.background),)
    spans: list[BrandSpan] = []
    if match.start():
        prefix = text[: match.start()]
        color = colors.text
        spans.append(BrandSpan(prefix, BrandRole.TEXT, color.foreground, color.background))
    matched = match.group()
    cursor = 0
    role_iter = iter((BrandRole.PRIMARY, BrandRole.SECONDARY, BrandRole.ACCENT))
    for index, character in enumerate(matched):
        if character.casefold() in {"g", "u", "c"}:
            if index > cursor:
                color = colors.text
                spans.append(
                    BrandSpan(
                        matched[cursor:index], BrandRole.TEXT, color.foreground, color.background
                    )
                )
            role = next(role_iter)
            color = colors.for_role(role)
            spans.append(BrandSpan(character, role, color.foreground, color.background))
            cursor = index + 1
    if cursor < len(matched):
        color = colors.text
        spans.append(
            BrandSpan(matched[cursor:], BrandRole.TEXT, color.foreground, color.background)
        )
    if match.end() < len(text):
        color = colors.text
        spans.append(
            BrandSpan(text[match.end() :], BrandRole.TEXT, color.foreground, color.background)
        )
    return tuple(spans)


def slim_brand(
    theme: ThemeDefinition,
    *,
    behavior: BrandColorBehavior = BrandColorBehavior.GERMAN_FLAG,
    capability: ColorCapability = ColorCapability.TRUECOLOR,
) -> RenderedBranding:
    """Return the exact, permanently slim ``GUC Mail CLI`` semantic title."""

    colors, adjusted = resolve_brand_colors(theme, behavior, capability)
    return RenderedBranding(
        preset_id="builtin.slim-title",
        variant=BrandSize.MINIMAL,
        lines=(BrandLine(_semantic_spans(SLIM_TITLE, colors)),),
        color_behavior=behavior,
        accessible_color_adjustment=adjusted,
    )


# Alpha/color samples from the supplied logo, arranged as terminal Braille
# pixels. Black source pixels use the theme's text color so the
# outline remains visible on both light and dark backgrounds.
_GUC_MARK_FULL: Final = (
    "................RRRRRR..",
    ".............RRRRRRRRRR.",
    "...........RRRRRRRRRRRR.",
    ".........RRRRRRRRRRRRRR.",
    ".......RRRR.......RRRRR.",
    "......RRR........RRRRRKK",
    ".....RR..........RRRR.KK",
    "...RRR..........RRRR..KK",
    "...YY.........RRRRR...KK",
    ".YYYYYYY.....RRRR.....KK",
    "YYYYYYYYYYY.RRRR......KK",
    "YYYYYYYYYYYYY.KK......KK",
    ".YYYYY....YYYYKK......KK",
    ".YYYYY.......YKKK.....KK",
    "..YYYY........KKK.....KK",
    "..YYYY........KKK....KKK",
    "...YYYY........KK....KK.",
    "....YYYY...YY..KKK..KKK.",
    ".....YYYY...YYYKKKKKKKK.",
    "......YYYYY.YYYKKKKKKKK.",
    "........YYYYYYY.KKKKKK..",
    "..........YYYYY.KKKKKK..",
    "............YYYY.KKKK...",
    "..................KK....",
)
_GUC_MARK_COMPACT: Final = (
    "..........RRRRR.",
    ".......RRRRRRRRR",
    "......RRRR.RRRRR",
    "....RRR.....RRRK",
    "...RR......RRRKK",
    "..RR......RRR.KK",
    "YYYYYY..RRR...KK",
    "YYYYYYYYYRK...KK",
    "YYYY...YYYK...KK",
    ".YYY.....KK...KK",
    "..YY......KK..KK",
    "..YYY...Y.KK..KK",
    "...YYY..YYKKKKK.",
    ".....YYYYYKKKKK.",
    ".......YYYYKKKK.",
    ".........Y.KKK..",
)


def _render_original_mark(
    preferences: BrandingPreferences,
    theme: ThemeDefinition,
    *,
    terminal_width: int,
    max_height: int,
    unicode_supported: bool,
    capability: ColorCapability,
) -> RenderedBranding:
    width = min(terminal_width, preferences.target_width or terminal_width)
    height = min(max_height, preferences.target_height or max_height)
    can_color = (
        unicode_supported
        and capability is not ColorCapability.MONOCHROME
        and preferences.color_behavior is not BrandColorBehavior.MONOCHROME
        and preferences.size is not BrandSize.MINIMAL
    )
    pattern: tuple[str, ...] = ()
    variant = BrandSize.MINIMAL
    if can_color and height >= 6 and width >= 23 and preferences.size is not BrandSize.COMPACT:
        pattern, variant = _GUC_MARK_FULL, BrandSize.FULL
    elif can_color and height >= 4 and width >= 19:
        pattern, variant = _GUC_MARK_COMPACT, BrandSize.COMPACT
    if not pattern:
        title = SLIM_TITLE[:width]
        text_color = (
            None
            if capability is ColorCapability.MONOCHROME
            or preferences.color_behavior is BrandColorBehavior.MONOCHROME
            else theme.palette.primary_text
        )
        padding = max(0, width - len(title))
        if preferences.alignment is BrandAlignment.CENTER:
            title = " " * (padding // 2) + title
        elif preferences.alignment is BrandAlignment.RIGHT:
            title = " " * padding + title
        return RenderedBranding(
            preset_id="builtin.guc-original-mark",
            variant=BrandSize.MINIMAL,
            lines=(BrandLine((BrandSpan(title, BrandRole.TEXT, text_color),)),),
            color_behavior=preferences.color_behavior,
            fallback_used=preferences.size is not BrandSize.MINIMAL,
            fallback_reason=(
                "The colored mark needs Unicode, color, and at least 19 columns by 4 rows."
                if preferences.size is not BrandSize.MINIMAL
                else None
            ),
        )

    red, adjusted_red = _legible_brand_color("#ed1c24", theme.palette.header)
    gold, adjusted_gold = _legible_brand_color("#ffcb08", theme.palette.header)
    pixel_colors = {"R": red, "Y": gold, "K": theme.palette.primary_text}
    pixel_roles = {"R": BrandRole.SECONDARY, "Y": BrandRole.ACCENT, "K": BrandRole.TEXT}
    mark_width = len(pattern[0]) // 2
    text_width = mark_width + len("  GUC MAIL")
    available = max(0, width - text_width)
    padding = (
        available // 2
        if preferences.alignment is BrandAlignment.CENTER
        else available
        if preferences.alignment is BrandAlignment.RIGHT
        else 0
    )
    lines: list[BrandLine] = []
    # Braille cells encode a 2-by-4 pixel grid, preserving more of the mark's
    # curves than solid blocks at the same terminal height. A cell has one
    # foreground color, so mixed-color cells use their most frequent source color.
    dot_bits = ((1, 8), (2, 16), (4, 32), (64, 128))
    for row in range(0, len(pattern), 4):
        spans: list[BrandSpan] = []
        if padding:
            spans.append(BrandSpan(" " * padding, BrandRole.TEXT, theme.palette.primary_text))
        for column in range(0, len(pattern[0]), 2):
            bits = 0
            counts = {"R": 0, "Y": 0, "K": 0}
            for dy, pair in enumerate(dot_bits):
                for dx, bit in enumerate(pair):
                    pixel = pattern[row + dy][column + dx]
                    if pixel != ".":
                        bits |= bit
                        counts[pixel] += 1
            if bits:
                color = max(counts, key=lambda key: counts[key])
                spans.append(BrandSpan(chr(0x2800 + bits), pixel_roles[color], pixel_colors[color]))
            else:
                spans.append(BrandSpan(" ", BrandRole.TEXT, None))
        label = "  GUC MAIL" if row == 4 else "  CLI" if row == 8 else ""
        if label:
            spans.append(BrandSpan(label, BrandRole.TEXT, theme.palette.primary_text))
        lines.append(BrandLine(tuple(spans)))
    return RenderedBranding(
        preset_id="builtin.guc-original-mark",
        variant=variant,
        lines=tuple(lines),
        color_behavior=preferences.color_behavior,
        accessible_color_adjustment=adjusted_red or adjusted_gold,
    )


def render_branding(
    preferences: BrandingPreferences,
    theme: ThemeDefinition,
    *,
    terminal_width: int,
    max_height: int = 8,
    unicode_supported: bool = True,
    capability: ColorCapability = ColorCapability.TRUECOLOR,
    catalog: BrandingCatalog | None = None,
) -> RenderedBranding:
    """Render a built-in/custom/hidden brand with adaptive sizing and alignment."""

    if terminal_width <= 0 or max_height <= 0:
        raise BrandingValidationError("Terminal geometry must be positive")
    if not preferences.visible or preferences.preset_id == "none":
        return RenderedBranding(
            preset_id="none",
            variant=BrandSize.MINIMAL,
            lines=(),
            color_behavior=preferences.color_behavior,
        )
    if preferences.preset_id == "builtin.guc-original-mark":
        return _render_original_mark(
            preferences,
            theme,
            terminal_width=terminal_width,
            max_height=max_height,
            unicode_supported=unicode_supported,
            capability=capability,
        )
    variant = _variant_for(
        preferences.size,
        terminal_width=terminal_width,
        max_height=max_height,
    )
    selected_catalog = catalog or BrandingCatalog()
    roles: tuple[BrandRole, ...]
    font_backend: str | None = None
    font_name: str | None = None
    fallback_used = False
    fallback_reason: str | None = None
    if preferences.preset_id == "custom":
        custom = sanitize_custom_ascii(
            preferences.custom_ascii,
            unicode_supported=unicode_supported,
        )
        lines = tuple(custom.splitlines()) if custom else (SLIM_TITLE,)
        roles = tuple(BrandRole.TEXT for _ in lines)
        preset_id = "custom"
    else:
        preset = selected_catalog.get(preferences.preset_id)
        lines = preset.lines_for(variant)
        preset_id = preset.id
        font_backend = preset.font_backend
        font_name = preset.font_name
        fallback_used = preset.fallback_reason is not None
        fallback_reason = preset.fallback_reason
        if variant is BrandSize.FULL:
            roles = preset.full_roles
        else:
            roles = tuple(
                (BrandRole.PRIMARY, BrandRole.SECONDARY, BrandRole.ACCENT, BrandRole.TEXT)[
                    index % 4
                ]
                for index in range(len(lines))
            )
    if not unicode_supported:
        lines = tuple(
            "".join(character if ord(character) <= 0x7E else "?" for character in line)
            for line in lines
        )
    target_width = preferences.target_width
    target_height = preferences.target_height
    if target_width is not None or target_height is not None:
        natural_width = max((len(line) for line in lines), default=1)
        resized_width = min(terminal_width, target_width or natural_width)
        resized_height = min(max_height, target_height or len(lines))
        lines = resize_ascii_art(lines, resized_width, resized_height)
        roles = tuple(
            roles[min(len(roles) - 1, (index * len(roles)) // len(lines))]
            for index in range(len(lines))
        )
    elif len(lines) > max_height or max((len(line) for line in lines), default=0) > terminal_width:
        lines = resize_ascii_art(
            lines,
            min(terminal_width, max(len(line) for line in lines)),
            min(max_height, len(lines)),
        )
        roles = tuple(
            roles[min(len(roles) - 1, (index * len(roles)) // len(lines))]
            for index in range(len(lines))
        )
    colors, adjusted = resolve_brand_colors(theme, preferences.color_behavior, capability)
    rendered: list[BrandLine] = []
    for index, line in enumerate(lines):
        if variant is BrandSize.MINIMAL or (len(lines) == 1 and "guc" in line.casefold()):
            spans = _semantic_spans(line, colors)
        else:
            role = roles[index % len(roles)] if roles else BrandRole.TEXT
            color = colors.for_role(role)
            spans = (BrandSpan(line, role, color.foreground, color.background),)
        width = len(line)
        available = max(0, terminal_width - width)
        padding = 0
        if preferences.alignment is BrandAlignment.CENTER:
            padding = available // 2
        elif preferences.alignment is BrandAlignment.RIGHT:
            padding = available
        if padding:
            text_color = colors.text
            spans = (
                BrandSpan(" " * padding, BrandRole.TEXT, text_color.foreground, None),
                *spans,
            )
        rendered.append(BrandLine(tuple(spans)))
    return RenderedBranding(
        preset_id=preset_id,
        variant=variant,
        lines=tuple(rendered),
        color_behavior=preferences.color_behavior,
        accessible_color_adjustment=adjusted,
        font_backend=font_backend,
        font_name=font_name,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


class BrandingSession:
    """Stage branding changes for live preview, exact cancel, and durable apply."""

    def __init__(
        self,
        preferences: BrandingPreferences | None = None,
        *,
        persist: Callable[[BrandingPreferences], None] | None = None,
    ) -> None:
        self.preferences = preferences or BrandingPreferences()
        self._preview: BrandingPreferences | None = None
        self._persist = persist

    @property
    def current(self) -> BrandingPreferences:
        return self._preview or self.preferences

    @property
    def is_previewing(self) -> bool:
        return self._preview is not None

    def preview(self, **changes: object) -> BrandingPreferences:
        try:
            candidate = replace(self.current, **changes)  # type: ignore[arg-type]
        except TypeError as exc:
            raise BrandingValidationError("Unknown branding preference") from exc
        _validate_preferences(candidate)
        self._preview = candidate
        return candidate

    def cancel(self) -> BrandingPreferences:
        self._preview = None
        return self.preferences

    def apply(self) -> BrandingPreferences:
        candidate = self._preview or self.preferences
        _validate_preferences(candidate)
        if self._persist is not None:
            self._persist(candidate)
        self.preferences = candidate
        self._preview = None
        return candidate

    def reset(self) -> BrandingPreferences:
        self._preview = BrandingPreferences()
        return self._preview


def _validate_preferences(preferences: BrandingPreferences) -> None:
    if preferences.target_width is not None and not 1 <= preferences.target_width <= _MAX_ART_WIDTH:
        raise BrandingValidationError(f"Target width must be 1..{_MAX_ART_WIDTH}")
    if (
        preferences.target_height is not None
        and not 1 <= preferences.target_height <= _MAX_ART_HEIGHT
    ):
        raise BrandingValidationError(f"Target height must be 1..{_MAX_ART_HEIGHT}")
    if preferences.preset_id == "custom":
        sanitize_custom_ascii(preferences.custom_ascii)
    elif preferences.preset_id != "none":
        BrandingCatalog().get(preferences.preset_id)


def validate_theme_branding(theme: ThemeDefinition) -> tuple[str, ...]:
    """Return accessibility warnings specific to theme-linked logo colours."""

    warnings: list[str] = []
    for name, color in (
        ("logo_primary", theme.palette.logo_primary),
        ("logo_secondary", theme.palette.logo_secondary),
        ("logo_accent", theme.palette.logo_accent),
    ):
        if contrast_ratio(color, theme.palette.header) < 3.0:
            warnings.append(f"{name} is below 3:1 against the header")
    return tuple(warnings)
