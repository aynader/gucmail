"""Protocol entry point: one canonical local message, read-only, no CLI options."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from gucmail.errors import ConfigurationError, GUCmailError
from gucmail.security import redact_text, sanitize_terminal
from gucmail.services.windows_desktop import (
    DesktopIdentity,
    WindowsDesktopService,
    account_fingerprint,
    activation_reference,
    read_desktop_identity,
)


def prepare_activation(arguments: Sequence[str]) -> tuple[DesktopIdentity, str]:
    # A quote inserted into a protocol URI must never become another Python or
    # gucmail option. No argparse/Typer command dispatch occurs here.
    if len(arguments) != 2:
        raise ConfigurationError("Desktop activation requires one registered manifest and one URI.")
    identity = read_desktop_identity(Path(arguments[0]))
    reference = activation_reference(identity, arguments[1])
    inspection = WindowsDesktopService(identity).inspect()
    if not inspection.known or not inspection.installed:
        raise ConfigurationError(
            "Desktop activation registration is unavailable. Review desktop setup."
        )
    return identity, reference


def open_activation(identity: DesktopIdentity, reference: str) -> None:
    from gucmail.bootstrap import AppContext
    from gucmail.tui.notification_reader import NotificationReaderApp

    for variable, path in (
        ("GUCMAIL_CONFIG", identity.config),
        ("GUCMAIL_CONFIG_DIR", identity.config.parent),
        ("GUCMAIL_DATA_DIR", identity.data_dir),
        ("GUCMAIL_CACHE_DIR", identity.cache_dir),
        ("GUCMAIL_LOG_DIR", identity.log_dir),
    ):
        os.environ[variable] = str(path)
    context = AppContext.create(config_path=identity.config)
    try:
        if (
            account_fingerprint(context.config.email or "", context.config.endpoint)
            != identity.account
        ):
            raise ConfigurationError(
                "The configured account changed; this notification cannot be opened."
            )
        # Check the existing local mapping before any credential or server work.
        mapping = context.state.resolve_reference(reference, item_kind="message")
        if mapping is None or mapping["item_kind"] != "message":
            raise ConfigurationError("The notification's local message reference is unavailable.")
        # Secure prompts use the new console before Textual takes over. Adapter
        # construction is lazy; the worker makes the selected-message request.
        context.gateway(interactive=True, allow_legacy=False)
        NotificationReaderApp(context, identity.account, reference).run()
    finally:
        context.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        identity, reference = prepare_activation(tuple(sys.argv[1:] if argv is None else argv))
        open_activation(identity, reference)
        return 0
    except GUCmailError as exc:
        print(redact_text(sanitize_terminal(str(exc), max_length=700)), file=sys.stderr)
        return int(exc.exit_code)
    except (OSError, ValueError) as exc:
        print(redact_text(sanitize_terminal(str(exc), max_length=700)), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "Desktop activation could not finish. Inspect desktop setup and local diagnostics.",
            file=sys.stderr,
        )
        return 12


if __name__ == "__main__":
    raise SystemExit(main())
