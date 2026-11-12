"""Textual full-screen user interface."""

from gucmail.tui.runtime import configure_styles_cache, configure_windows_sleep

configure_windows_sleep()
configure_styles_cache()

from gucmail.tui.app import GUCmailApp, run_tui  # noqa: E402

__all__ = ["GUCmailApp", "run_tui"]
