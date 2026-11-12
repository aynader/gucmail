"""Reproduce the built-in theme asset from authoritative recipes, never user settings."""

import argparse
import gzip
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from gucmail.tui.themes import GENERATOR_VERSION, _generate_builtin_themes, _payload_for_theme


def catalogue_bytes():
    themes = _generate_builtin_themes()
    payload = json.dumps(
        {
            "schema": "gucmail.builtin-themes/v1",
            "generator": GENERATOR_VERSION,
            "themes": [_payload_for_theme(theme) for theme in themes],
        },
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    compressed = bytearray(gzip.compress(payload, compresslevel=9, mtime=0))
    # Canonical gzip OS byte: Python/zlib platforms must generate the same asset.
    compressed[9] = 255
    return bytes(compressed), len(payload), len(themes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data, decoded_bytes, count = catalogue_bytes()
    if args.check:
        assert files("gucmail").joinpath("data", "builtin-themes.json.gz").read_bytes() == data
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(data)
    print(
        json.dumps(
            {
                "bytes": len(data),
                "decoded_bytes": decoded_bytes,
                "definitions": count,
                "sha256": hashlib.sha256(data).hexdigest(),
                "checked": args.check,
            }
        )
    )


if __name__ == "__main__":
    main()
