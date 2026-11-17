"""The installed `gucmail` command. Answers version checks before loading the full CLI."""

import sys

from gucmail import __version__


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(__version__)
        return
    if args == ["version"]:
        print(f"gucmail {__version__}")
        return
    from gucmail.cli import app

    app()


if __name__ == "__main__":
    main()
