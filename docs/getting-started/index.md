# Getting started

**Audience:** first-time and nontechnical users  
**Mode:** Basic  
**Platforms:** Windows 10/11 first; Linux and macOS are also declared supported  
**Connection:** installation is local; login and live mailbox actions need the network

GUC Mail CLI is an independent terminal client for a GUC Exchange mailbox. It is **not an
official GUC or Microsoft product**. This guide gets you from a clean computer to reading a
message safely, without putting a password in a command or configuration file.

The examples use fictional addresses under `example.invalid`. Never paste a real password into a
command, a screenshot, an issue, or a support bundle.

## What you will do

1. Open PowerShell or Command Prompt.
2. Install an isolated copy with `pipx`.
3. Sign in through a hidden password prompt and the operating-system keyring.
4. Open the full-screen terminal interface (TUI).
5. Read a message and review how sending works.
6. Learn how to exit, update, and remove the stored credential.

Allow about ten minutes. Installation does not contact the mail server. Login performs a small,
bounded authentication check; ordinary documentation tests never send mail.

## Before you start

You need:

- Windows 10 or 11, Linux, or macOS;
- Python 3.11 through 3.14 (Python 3.11 is used below because it is the most conservative choice);
- your account email, login name if it differs from the email, and password;
- network access to the configured Exchange Web Services endpoint; and
- a terminal at least 80 columns wide for a comfortable TUI. A 120-column terminal shows all
  three panes.

Server version, account permissions, calendar access, directory access, exact quota reporting,
and native Exchange features vary between accounts. An unsupported or unauthorized result means
the requested operation was not performed.

## Open a terminal on Windows

### PowerShell (recommended)

1. Open the **Start** menu.
2. Type **PowerShell** or **Windows Terminal**.
3. Open the matching application.

You will see a prompt similar to `PS C:\Users\Example>`. Type only the commands shown after the
prompt; do not type the prompt itself.

### Command Prompt

1. Open the **Start** menu.
2. Type **Command Prompt** or **cmd**.
3. Open the application.

The `py` and `pipx` commands below work in either shell.

## Install with pipx

`pipx` installs the application in its own Python environment while making the `gucmail` command
available to your user account.

Check Python, install `pipx`, and add its application directory to your path:

```powershell
py -3.11 --version
py -3.11 -m pip install --user pipx
py -3.11 -m pipx ensurepath
```

Close every terminal window and open PowerShell or Command Prompt again, then install the
`gucmail` package from the Python Package Index (PyPI) and check it:

```text
pipx install "gucmail[desktop]"
gucmail --version
gucmail --help
```

The `[desktop]` extra adds desktop notifications. Other optional extras are `attachments`
(text extraction from attachments for local search) and `semantic` (optional local semantic
search); combine them as `"gucmail[desktop,attachments]"`. Plain `pipx install gucmail` installs
the core application only.

### Virtual-environment fallback

If `pipx` is unavailable, create a virtual environment in a folder of your choice and use it
without activating it:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install "gucmail[desktop]"
.\.venv\Scripts\gucmail.exe --help
```

Use that environment's Python for the module fallback:

```powershell
.\.venv\Scripts\python.exe -m gucmail --help
```

This explicit path prevents Windows from silently choosing a different Python installation.

### Linux or macOS

From a terminal:

```sh
python3 --version
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

Open a new terminal, then run:

```sh
pipx install "gucmail[desktop]"
gucmail --help
```

The CLI and TUI are supported on Linux and macOS. Platform keyrings and desktop notifications
differ, so read any provider-specific message rather than assuming Windows behavior.

### Install from a source checkout

If you have a downloaded copy of the source, you can install it instead. From the source
directory, run `pipx install .`, or `python -m pip install .` inside a virtual environment. Here
`.` means “the project in this directory”; nothing is uploaded anywhere.

## First launch and secure login

Start the guided setup:

```text
gucmail login
```

The setup explains that the client is unofficial, asks for an email and optional separate login
name, and obtains the password through a hidden prompt. Pressing Enter for the login name uses the
email address. For example, documentation and practice data should look like:

```text
Email: student@example.invalid
Login username: student.example
Password: [hidden]
```

The password is not a field in `config.toml`. When keyring storage is selected, it is stored under
the application service name in the operating-system keyring (Windows Credential Manager on
Windows). Environment variable passwords are intended only for a single, controlled process and
are not the recommended everyday login path.

Other non-secret settings remain editable with `gucmail config show`, `gucmail config set`, and the
configuration file reported by `gucmail config path`. Defaults are conservative:

- time zone is `Africa/Cairo`;
- remote content remains blocked;
- monitoring is off;
- attachment indexing is off;
- semantic search is off; and
- setup does not download the whole mailbox.

Only enable indexing or monitoring after reading their privacy and storage explanations. See
[Privacy and security](../privacy-and-security/index.md).

### If a legacy `creds.env` file is detected

Treat it as a plaintext secret. The setup reports detection without displaying the contents. It can
parse only quoted `email`, `username`, and `password` values as data; it must never execute the file
as Python.

1. Do not print, attach, execute, or commit the file.
2. This login command does not migrate it automatically. Enter the credential through the hidden
   prompt so the password is stored in the OS keyring.
3. Complete the bounded authenticated check.
4. Delete the plaintext file yourself only after that verification.

The application never deletes the source file automatically.

## Open the terminal interface

```text
gucmail tui
```

With the virtual-environment fallback:

```powershell
.\.venv\Scripts\python.exe -m gucmail tui
```

The wide layout has a folder tree, message list, and safe preview. Medium terminals hide the preview
pane; narrow terminals show the message list only. Resize the window if information feels cramped,
or use the command interface when a narrow terminal hides navigation.

### Essential controls

| Action | Keyboard | Mouse |
|---|---|---|
| Move within a pane | Arrow keys, Page Up/Down, Home/End | Scroll or click a row |
| Change pane | Tab / Shift+Tab | Click the pane |
| Open the selected item | Enter | Click or double-click as shown by the widget |
| Return focus to the message list | Escape or Backspace | Click the message list |
| Search/filter | `/` | Click the filter box |
| Compose | `c` | Choose Compose |
| Reply / reply all / forward | `r` / `a` / `f` | Choose the matching action |
| Mark read or unread | `u` | Keyboard action |
| Refresh | F5 | Keyboard action |
| Command palette | Ctrl+K | Keyboard action |
| Help | `?` | Keyboard action |
| Exit safely | `q` | Close the application normally |

The mouse is optional. The footer shows the actions available in the focused view.

For optional practice before signing in, run `gucmail tui --offline`, press `?`, and choose
**Tutorials**. Seven interactive lessons cover navigation, preparing a message, the command
palette, natural-language search, scheduling, notifications and backups. **Check** validates
the exercise; **Next** advances and **Finish** records completion. Escape skips the lesson;
**Restart** clears only that lesson's progress. **Guide** opens the full manual and returns to
the same step when closed. Exercises are synthetic and cannot send or queue mail, make a backup,
deliver a notification or contact Exchange.

## Read your first message

1. Select **Inbox** in the folder tree.
2. Move to a message with the arrow keys or mouse.
3. Press Enter to load the full message into the preview.
4. Check the status bar for **Message loaded without remote content**.
5. Press `u` only if you deliberately want to change its read state.

The preview renders sanitized text. It does not load scripts, frames, tracking pixels, or remote
images. Merely opening a message does not request a read-state change.

The equivalent read-only commands are:

```text
gucmail messages list --folder inbox --limit 25
gucmail messages show MESSAGE_REFERENCE
```

Copy `MESSAGE_REFERENCE` from list output. References are opaque identifiers; do not edit or guess
them.

## Send a first message carefully

Use a recipient you control for your first real test. The fictional address below is deliberately
undeliverable.

### In the TUI

1. Press `c`.
2. Fill in **To**, **Subject**, and **Body**. Cc, Bcc, and importance are optional.
3. Choose **Review send**.
4. Read the recipient count and subject in the confirmation dialog.
5. Choose **Cancel** to practice safely, or **Confirm** only when the actual recipient is correct.
6. Watch the status, then refresh **Sent** or use the command interface to inspect scheduled jobs.

A “send requested” message is not proof of delivery. If the connection fails after Exchange may
have accepted a message, the client records an uncertain state instead of blindly sending a
duplicate.

### From the command line

First inspect the current options:

```text
gucmail send --help
```

The basic form is:

```text
gucmail send --to classmate@example.invalid --subject "Synthetic example" --body "Hello"
```

Sending is a mutating online action and requires the command's review/confirmation policy. Do not
use the fictional command as a live test.

## Understand where a message is

These labels are intentionally different:

| Label | Meaning |
|---|---|
| Local recovery | A crash-recovery copy on this computer; it is not a server draft |
| Draft | Editable and not sent; the details show whether it is local, server, synchronized, or conflicted |
| Scheduled | Stored locally for a future attempt; the local scheduler must run |
| Outbox | Queued, retrying, waiting for connectivity/authentication, sending, uncertain, or awaiting review |
| Sent | The send outcome was confirmed; this is different from merely starting a request |
| Backup | A separate offline copy; it is not the disposable cache or search index |

If a send is **uncertain** or requires **manual review**, inspect Sent and the job history before
taking action. Never create a second copy just to “try again.”

## Next useful workflows

| Goal | Starting point |
|---|---|
| Save or resume a local draft | Compose → **Save draft**, then `gucmail drafts list` |
| Download an attachment | Open the message in the TUI, then `gucmail attachments list MESSAGE_REFERENCE` |
| Schedule a saved draft | `gucmail drafts schedule DRAFT_REFERENCE --at "2026-08-06T09:00:00+03:00"` |
| Filter the current TUI page | Press `/`; use `gucmail search --help` for server/index search |
| Check local storage | Open **Storage**; use `gucmail quota show` for exact server quota |
| View upcoming events | Open **Calendar** or run `gucmail calendar agenda` |
| Browse people | Open **Contacts**; use `gucmail contacts search "Example Person"` to search |
| Inspect backups | Open **Backups**; use `gucmail backup estimate` to plan one |
| Review notification results | Open **Activity** or run `gucmail notifications history` |

The TUI provides mail navigation, safe preview, compose/reply/forward, local draft save, recoverable
deletion, read-state changes, filtering, and guided editing in Calendar, Contacts and Tasks.
Schedules, backups, storage, notifications and Settings also have dedicated workflows. Reads are
bounded and changes use the relevant review or confirmation. Server capabilities and account
permissions still determine which Exchange operations succeed. The command interface exposes
scriptable editing and automation; use `gucmail <group> --help` for its exact options and read an
unsupported/unauthorized result literally.

Use `gucmail tui --offline` to inspect local schedules, backups, storage, activity, and redacted
settings without resolving credentials or Exchange. Online folders, calendar, contacts, and tasks
then show an unavailable status instead of opening a network session.

Continue with the [complete user guide](../user-guide/index.md), especially the dedicated
[scheduled-email guide](../scheduling/index.md) before relying on unattended sending.

## Exit safely

Press `q` in the TUI. A normal exit does not delete drafts, scheduled jobs, local state, the
keyring credential, or backups. It also does not make scheduled mail run: a local scheduler task or
foreground scheduler still has to execute.

If a foreground watcher or scheduler is running in a terminal, press Ctrl+C once and wait for its
summary. Closing the TUI does not necessarily stop a separately installed OS task.

## Update the application

Check the installed version first:

```text
gucmail --version
```

Update a `pipx` installation from PyPI:

```text
pipx upgrade gucmail
```

For the virtual-environment installation:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade "gucmail[desktop]"
```

If you installed from a source checkout, update the source, return to it, and run
`pipx install --force .` (or `python -m pip install --upgrade .` in a virtual environment).

An update does not intentionally remove local state. Back up important data before a major version
change and read any migration notice.

## Remove a stored credential or uninstall

To remove the selected account password from the OS keyring:

```text
gucmail logout
```

`logout` does not delete mail, local drafts, scheduled jobs, cache, index, diagnostics, or backups.
Review those separately before removing anything. To remove a `pipx` installation:

```text
pipx uninstall gucmail
```

Uninstalling the program still does not promise to erase local data or backups. See
[Local data and retention](../privacy-and-security/local-data-locations-and-retention.md) for a
scoped cleanup procedure.

## If something goes wrong

- **`gucmail` is not recognized:** open a new terminal, run `py -3.11 -m pipx ensurepath`, open a
  new terminal again, and check `py -3.11 -m pipx list`.
- **The wrong Python runs:** use the explicit `.\.venv\Scripts\python.exe` path shown above.
- **Login is rejected:** re-run `gucmail login`; do not put the replacement password on the command
  line. A changed or expired password must also be changed through the institution's supported
  account process.
- **The keyring is unavailable:** read the reported provider error. Do not fall back to storing a
  password in `config.toml` or a script.
- **The TUI says offline or unavailable:** press F5 once after checking the network. Repeated rapid
  refreshes can worsen throttling.
- **The layout is cramped:** widen the terminal to at least 80 columns, preferably 120.
- **A feature says unsupported or unauthorized:** nothing was changed. Check the support matrix
  instead of retrying a mutating command.

Run the read-only diagnostic summary when available:

```text
gucmail doctor
```

Then use [Troubleshooting by symptom](../troubleshooting/index.md). Never attach a credential file,
raw mailbox content, or an unreviewed diagnostic bundle to a support request.

## Related topics

- [User guide](../user-guide/index.md)
- [Scheduling](../scheduling/index.md)
- [Search](../search/index.md)
- [Backup and offline use](../backup/index.md)
- [Privacy and security](../privacy-and-security/index.md)
- [Command reference](../command-reference/index.md)
- [Glossary](../glossary/index.md)
