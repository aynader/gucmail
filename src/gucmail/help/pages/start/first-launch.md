# First launch on Windows

GUC Mail CLI can open a full-screen terminal interface or run one scriptable command at a time. The application is independent from the university and Microsoft. It connects directly to the configured GUC Exchange HTTPS endpoint and keeps passwords out of ordinary configuration.

## Install or use the project environment

From the project directory in Windows PowerShell, activate the existing virtual environment if needed, then check the installed executable without contacting Exchange:

```text
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\gucmail.exe version
.\.venv\Scripts\gucmail.exe doctor
```

If PowerShell execution policy prevents activation, activation is optional; call `.\.venv\Scripts\gucmail.exe` directly. A packaged installation exposes the shorter `gucmail` command. `doctor` is local by default. Network and authentication checks require their explicit options.

## Recommended credential setup

Run `gucmail login`, confirm the mailbox identity and username, and enter the password through the hidden prompt. On Windows the recommended store is Windows Credential Manager through the operating-system keyring. Normal configuration contains the email, username, endpoint, and preferences but never the plaintext password.

Then launch:

```text
.\.venv\Scripts\gucmail.exe tui
```

Credential priority is deliberate. An explicitly selected credential source takes precedence, followed by controlled environment credentials, the OS keyring, an interactive hidden prompt, and finally the legacy `creds.env` compatibility path. This means a plain TUI launch can use `creds.env` from the current working directory only when a higher-priority source did not already provide the password.

To state the legacy file explicitly, put the global option before `tui`:

```text
.\.venv\Scripts\gucmail.exe --credentials-file .\creds.env tui
```

The file is parsed as data and is never executed as Python. It remains plaintext, so protect its permissions, never commit it, never show it in screenshots or logs, and migrate to the keyring when practical. GUC Mail never prints the password.

## Learn the interface

The header includes GUC branding, connection and storage status, while the main mail view contains the folder tree, bounded message list, reader, and status/footer controls. Use `Tab` and `Shift+Tab` to move focus, arrows to navigate, `Enter` to open, `Escape` to return, `?` for contextual Help, and `Ctrl+K` for the universal command palette.

Start by opening Inbox and a message you are comfortable viewing. Reading does not intentionally mark it read. Remote images and active HTML stay blocked. Open Compose with `c`, but cancel a synthetic practice message before sending until you understand the review and confirmation step.

## If the first connection fails

Stop after one failure. Check the configured email, username, endpoint, Windows clock, network, and whether the password changed. Use `gucmail status` for local state, then deliberately choose one bounded online status or Doctor check. Repeated authentication attempts can trigger throttling or account protection.

Continue with [Sign in and protect your password](help:start.login), the [Interface tour](help:start.interface), and the [first-message tutorial](help:tutorial.first-message).
