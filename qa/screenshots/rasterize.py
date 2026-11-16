"""Rasterize Rich SVG terminal exports to PNG with headless Edge (local files only)."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE = Path(tempfile.gettempdir()) / "gucmail-screenshot-edge-profile"


def prepare(svg: str) -> tuple[str, int, int]:
    svg = re.sub(r"@font-face\s*\{[^}]*\}", "", svg)
    svg = svg.replace(
        "font-family: Fira Code, monospace;", "font-family: 'Cascadia Mono', Consolas, monospace;"
    )
    svg = svg.replace("font-family: arial;", "font-family: 'Segoe UI', Arial, sans-serif;")
    if "http://" in svg.replace("http://www.w3.org", "") or "https://" in re.sub(
        r"<!--.*?-->", "", svg
    ):
        # Never let the browser resolve a remote resource.
        svg = re.sub(r"url\(\s*['\"]?https?://[^)]*\)", "none", svg)
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    width, height = int(float(match.group(1))), int(float(match.group(2)))
    return svg, width, height


def rasterize(source: Path, target: Path, scale: float = 1.0) -> None:
    svg, width, height = prepare(source.read_text(encoding="utf-8"))
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>html,body{margin:0;padding:0;"
        "background:transparent;overflow:hidden}svg{display:block}</style></head><body>"
        + svg.replace("<svg ", f'<svg width="{width}" height="{height}" ', 1)
        + "</body></html>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - fixed local Edge binary, local file only
            [
                EDGE,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-component-update",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--default-background-color=00000000",
                f"--user-data-dir={PROFILE}",
                f"--force-device-scale-factor={scale}",
                f"--window-size={width},{height}",
                f"--screenshot={target}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )


if __name__ == "__main__":
    out = Path(sys.argv[1]).resolve()  # Edge needs an absolute output path
    scale = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].endswith(".svg") else 1.0
    for name in [a for a in sys.argv[2:] if a.endswith(".svg")]:
        src = Path(name)
        rasterize(src, out / (src.stem + ".png"), scale)
        print("ok", src.stem)
