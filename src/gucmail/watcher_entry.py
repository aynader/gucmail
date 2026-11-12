"""Internal headless entry point with explicit, non-secret application paths."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path


def ensure_output_streams() -> None:
    """pythonw.exe (used by background tasks) starts with no stdout/stderr at all."""

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def bind_paths(values: argparse.Namespace) -> None:
    for name, variable in (
        ("config", "GUCMAIL_CONFIG"),
        ("data_dir", "GUCMAIL_DATA_DIR"),
        ("cache_dir", "GUCMAIL_CACHE_DIR"),
        ("log_dir", "GUCMAIL_LOG_DIR"),
    ):
        path = getattr(values, name).expanduser().resolve()
        os.environ[variable] = str(path)
    os.environ["GUCMAIL_CONFIG_DIR"] = str(values.config.expanduser().resolve().parent)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the watcher using selected application paths."
    )
    for name in ("config", "data-dir", "cache-dir", "log-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--forever", action="store_true")
    values = parser.parse_args(argv)
    ensure_output_streams()
    bind_paths(values)
    from gucmail.cli import app

    arguments = ["--non-interactive", "headless", "run", "--profile", values.profile]
    if values.forever:
        arguments.append("--forever")
    app(args=arguments, prog_name="gucmail")


if __name__ == "__main__":
    main()
