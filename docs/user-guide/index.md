# User guide

GUC Mail CLI has two interfaces over the same service and safety boundaries: the `gucmail`
command and the Textual TUI opened by `gucmail tui`. The command interface is the complete,
scriptable surface. The TUI focuses on mail navigation, safe preview, compose, reply, forward,
recoverable delete, read state, filtering, and bounded read-only calendar, contact, task, schedule,
backup, storage, activity, and redacted-settings views.

## Daily workflow

```text
gucmail status
gucmail folders tree
gucmail messages list --folder inbox --limit 25
gucmail messages show MESSAGE_REFERENCE
gucmail drafts create --to person@example.test --subject "Example" --body "Synthetic text"
gucmail drafts list
```

Listing fetches bounded summaries. Showing a message fetches its details without marking it read.
Remote images, scripts, frames, and tracking pixels are not loaded. References are opaque local
handles; do not edit them.

## Returning to the TUI

A normal close saves the last mail pane and a bounded row and vertical scroll position
in the first message page. The last folder and layout continue to use your existing
preferences. Positions are clamped to the available rows and terminal size; a pane hidden
by a compact layout falls back to the message list. The folder tree can restore a visible
scroll position without fetching additional folders. This restores a position, not a
particular message, message body, or previous search result.

Opening local Search can reuse your last choice of all local sources, the full-text index,
indexed attachments, or backup archives. It starts with an empty query and does not run a
search automatically. Explicit source-specific commands still choose their own source.
Send and delete confirmations, pending requests, and transient screens never reopen from
this session state. Unsent composition recovery remains in the dedicated draft workflow.

Session state is optional local data. A locked, unavailable, or corrupt state database,
or a newer session saved by another app, can leave the position unsaved; existing data is
preserved and mail remains usable. A forced process termination may lose the last position.

## Changes and confirmation

Read-only commands do not alter the mailbox. Marking read, moving, copying, categorizing, sending,
calendar/contact/task edits, restore, and cleanup are mutations. Ordinary delete moves an item to
Deleted Items. Permanent deletion is a separate option and is never the bulk default.

Use `--dry-run` or a command's preview mode where offered. In automation, add global
`--non-interactive`; a required confirmation then fails closed. Add global `--yes` only after the
exact scope has been reviewed.

## Structured output

Put global options before the command:

```text
gucmail --json messages list --folder inbox --limit 10
gucmail --ndjson index search "synthetic query"
gucmail --no-color quota local
```

JSON uses the versioned `gucmail.output/v1` envelope. NDJSON emits bounded item records and a final
summary. Errors use stable exit codes and never intentionally include credentials.

## Product areas

- [Calendar](../calendar/index.md)
- [Contacts](../contacts/index.md)
- [Tasks](../tasks/index.md)
- [Scheduling](../scheduling/index.md)
- [Search and local indexing](../search/index.md)
- [Backup and restore](../backup/index.md)
- [Monitoring and notifications](../monitoring/index.md)
- [Privacy and local data](../privacy-and-security/index.md)

Run `gucmail GROUP --help` and `gucmail GROUP COMMAND --help` for the exact installed options.
Server support and account permission remain distinct from local implementation status; inspect
`gucmail ews support-matrix` rather than inferring support from documentation.


Mail Search also remembers the last live scope: current folder, all mail folders, or a selected
folder. **Search Mail** reopens an empty prompt with that scope after restart; it does not run the
previous query. Explicit Current Folder/All Mail commands override the remembered choice.
Changing the default search source in Settings supersedes the old session choice. Local Search
retains its separate local source choice. Earlier session rows remain readable and gain the new
safe fields only when a valid session change is saved; existing data is preserved.
