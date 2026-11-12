"""Allow ``python -m gucmail`` to behave like the installed command."""

from gucmail.cli import app

if __name__ == "__main__":
    app()
