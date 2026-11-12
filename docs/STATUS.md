# Status and known limits

GUCmail works for daily mail on Windows. Some areas have only been checked against a
synthetic test mailbox, not the real GUC server. Please report problems along with
the exact command and the message you saw. Never include your password.

## What has been checked

| Area | State |
|---|---|
| Install from wheel, `gucmail` and `python -m gucmail` launchers | Checked locally (Windows 11, Python 3.11) |
| Mail: list, read, search, move, copy, delete, restore, flags, categories, export | Checked on a synthetic mailbox; core mail also works against the GUC server |
| Compose, reply, reply-all, forward, drafts, attachments | Checked on a synthetic mailbox; sending also works against the GUC server |
| Scheduled send, Undo Send, recurring messages, local scheduler | Checked with a synthetic backend; duplicate-send protection checked |
| Calendar, contacts, tasks | Checked on a synthetic mailbox, including the Cairo daylight-saving change |
| Backups (full, incremental, encrypted, verify, browse, restore plan) | Checked locally |
| Local index, search, insights, cleanup previews | Checked on a synthetic mailbox |
| Full-screen interface: every workspace, all palette routes, Settings, themes | Checked in Textual's built-in test mode at several sizes (not yet in a real Windows terminal) |

## Known limits

- **Server-dependent features.** These depend on what the GUC server and your account allow:
  directory search, free/busy, inbox rules, automatic replies, delegates and quota thresholds.
  If the server refuses one, GUCmail tells you so. It never shows made-up values.
- **Scheduled sending is local.** It needs this computer and either `gucmail scheduled run`
  or Windows Task Scheduler (see [Scheduling](scheduling/index.md)). If the computer is off,
  messages are sent the next time the scheduler runs.
- **Recurring messages** don't support end dates or specific-weekday rules yet.
- **Startup speed.** Starting the full-screen interface can take 2–4 seconds on large mailboxes.
- **Webhooks** accept only public HTTPS addresses, so local test servers are refused.
- **Re-importing** the same `.ics` file creates duplicate events.
- **Optional extras** (desktop notifications, attachment text extraction, semantic search,
  Windows OCR) must be installed separately and have had limited testing.
- **Platforms.** Linux and macOS should work but have not been tested yet.
