# User guide

gucmail has two faces over the same engine. `gucmail` opens the full-screen app. Commands like
`gucmail messages list` do the same things from a script.

## Keys in the mail window

| Key | Does |
|---|---|
| Arrows, Enter | Move, open |
| Tab | Next pane |
| c, r, a, f | Compose, reply, reply all, forward |
| d | Delete (moves to Deleted Items) |
| e | Archive |
| ! | Flag or unflag |
| s | Save attachments |
| u | Mark read or unread |
| x | Export selected messages |
| Space | Select several messages. d, e and ! then act on all of them |
| Ctrl+A | Select all messages. Press again to clear |
| Ctrl+Z | Undo the last delete, archive or flag |
| o | Show or hide the signature and earlier conversation in a reply |
| / | Search |
| Ctrl+K | Command palette |
| F5 | Refresh |
| ? | Every shortcut |

In the list, ● is unread, ⚑ is flagged and + has attachments.

Delete, archive and flag act at once and say so in the status bar. Ctrl+Z puts things back.
In Deleted Items, **d** asks first.

## From the command line

```text
gucmail status
gucmail folders tree
gucmail messages list --folder inbox --limit 25
gucmail messages show MESSAGE_REFERENCE
gucmail drafts list
```

Showing a message doesn't mark it read. Remote images, scripts and tracking pixels are never
loaded. References like `msg_…` are local handles. Copy them, don't edit them.

## Changes and confirmation

Reading never changes the mailbox. Moving, sending, editing and cleanup do. Commands that change
mail preview first. Add `--dry-run` to only preview. In scripts, add `--non-interactive` so a
needed confirmation fails instead of waiting, and `--yes` once you've checked the scope.
Permanent delete is always a separate, explicit option.

## Structured output

Put global options before the command.

```text
gucmail --json messages list --folder inbox --limit 10
gucmail --ndjson search --unread
gucmail --no-color quota
```

JSON uses the versioned `gucmail.output/v1` envelope. Errors use stable exit codes and never
include passwords.

## When you come back

The app reopens where you left it, with the same folder, layout and list position. It shows
your last inbox list at once and updates it from the server. Send and delete dialogs never
reopen by themselves.

## More

- [Calendar](../calendar/index.md)
- [Contacts](../contacts/index.md)
- [Tasks](../tasks/index.md)
- [Scheduling](../scheduling/index.md)
- [Search and local indexing](../search/index.md)
- [Backup and restore](../backup/index.md)
- [Monitoring and notifications](../monitoring/index.md)
- [Privacy and local data](../privacy-and-security/index.md)

`gucmail --help` lists everyday commands and `gucmail advanced --help` the rest. Add `--help`
to any command for its options.
