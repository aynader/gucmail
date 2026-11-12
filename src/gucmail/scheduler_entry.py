"""Run due scheduled sends against the explicitly selected local application state."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from gucmail.watcher_entry import bind_paths, ensure_output_streams


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run scheduled sends using selected app paths.")
    for name in ("config", "data-dir", "cache-dir", "log-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    ensure_output_streams()
    bind_paths(parser.parse_args(argv))
    from gucmail.cli import app

    app(args=["--non-interactive", "--quiet", "scheduler", "run-once"], prog_name="gucmail")


if __name__ == "__main__":
    main()
