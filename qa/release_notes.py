"""Print the CHANGELOG.md section for one version. Fails if there is none.

    python qa/release_notes.py 1.2.0
"""

from __future__ import annotations

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(version: str, text: str) -> str:
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(f"## {version}")), None
    )
    if start is None:
        raise SystemExit(f"CHANGELOG.md has no section for {version}.")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    body = "\n".join(lines[start + 1 : end]).strip()
    if not body:
        raise SystemExit(f"The CHANGELOG.md section for {version} is empty.")
    return body


if __name__ == "__main__":
    print(section(sys.argv[1], CHANGELOG.read_text(encoding="utf-8")))
