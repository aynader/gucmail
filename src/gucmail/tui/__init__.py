"""Textual full-screen user interface."""

from typing import Any

from gucmail.tui.runtime import configure_styles_cache, configure_windows_sleep

configure_windows_sleep()
configure_styles_cache()

__all__ = ["GUCmailApp", "run_tui"]


def __getattr__(name: str) -> Any:
    # Load the full app only when it is asked for, so importing one screen stays cheap.
    if name in __all__:
        from gucmail.tui import app

        return getattr(app, name)
    raise AttributeError(f"module 'gucmail.tui' has no attribute {name!r}")
