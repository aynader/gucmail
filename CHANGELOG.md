# Changelog

What changed in each release, newest first. Each release gets a short entry written for the
people using gucmail, not for its developers.

## 1.5.0 (2026-11-17)

### Improved
- Faster start. The app opens in about a second and shows your last inbox list right
  away, then updates it from the server. `gucmail version` answers instantly.
- Plain language everywhere. Times read "Today 10:00" or "Tue 14:00", folders show
  Outlook names, and empty screens say what to do next.
- Errors say what happened and what to do. A rejected password opens the sign-in screen.
- Quicker mail keys. **d** deletes and **e** archives without a dialog, **!** flags,
  **s** saves attachments and **Ctrl+Z** undoes the last one.
- Replies hide the signature and quoted earlier conversation. Press **o** to show them.
- **Space** selects several messages and **Ctrl+A** selects all. The status line lists
  what you can do with them.
- The reader lists attachment names and sizes, and shows names with addresses.
- The message list shows the date at normal window sizes, like "10:42", "Yesterday" or
  "4 Mar". ⚑ marks flagged mail and + marks attachments.
- **Send later** in the composer asks when and schedules the message.
- Help speaks plainly, and pages no longer start with a status banner.
- The header shows how much of your mailbox you use, like "Mailbox 74 MiB of 92 MiB
  (81%)". It adds up your folder sizes on the server and checks again every 10 minutes.
- Settings search keeps up with fast typing.
- `gucmail status` gives a short plain summary.
- `gucmail --help` lists the everyday commands. The rest moved to `gucmail advanced` and
  still work as before.

### Fixed
- **Alerts** in the header now says on when new-mail alerts run in the background.
- Moving mail to "Archive" now uses your Archive folder, not the separate online archive.

## 1.4.0

There was no 1.3.0. Its work is part of this release.

### Improved
- A sign-in screen on first start. Type your GUC email and password and press **Test &
  save**: gucmail checks them with one small request and saves the password only if they
  work, with a plain reason if not. A three-step tour follows (arrows, Enter, Ctrl+K).
  Before, a new user saw an empty inbox and "Mailbox unavailable".
- `gucmail login` now tests the password before saving it, so a wrong password is never
  stored.
- `gucmail` on its own opens the app (it used to print help). Scripts and pipes still get
  the help text.
- One switch for notifications. `gucmail notifications on` (or **Turn on notifications** in
  the Ctrl+K palette, or the **Notify me about new mail** switch in notification settings)
  registers gucmail with Windows, starts the background task and sends a test, then shows
  ✓ or ✗ for each step. `gucmail notifications off` turns them off.
- One background task instead of two. The new-mail task now also sends scheduled messages
  (checked every 30 seconds), and turning notifications on removes the old separate
  Scheduler task. Turning notifications off keeps the task running while scheduled mail
  is still waiting.
- `gucmail notifications status` gives a plain answer: on or off, and whether the task runs.

### Fixed
- Creating a backup on Windows no longer fails now and then with "Access is denied"
  when antivirus or the search indexer briefly holds a new file.
- F5 in the window that opens from a notification now refreshes as soon as the message is
  shown. It could be ignored for a moment before.
- Double-clicking Close in Keyboard shortcuts or Saved searches no longer also closes the
  screen behind it.
- Screens such as Drafts load faster on a busy computer: progress updates no longer hold up
  the loading work.

## 1.2.0

### Fixed
- Notifications: one privacy setting now decides what a toast shows. "(No subject)" appears
  only when a message really has no subject. Saved settings from 1.1 keep working.
- `saved-searches add` no longer replaces a search with the same name silently (use
  `--replace`), and refuses an empty search.
- Importing the same `.ics` file twice no longer duplicates events.
- `calendar edit --start` without `--end` keeps the event's length.
- Fuzzy search finds one-letter typos ("ahmad" finds "Ahmed", "exma" finds "exam").
- The command palette shows each command once.
- On Python 3.12 and newer, "next page" for contacts and tasks, and directory lookup, no
  longer fail with "does not support continuation" or "does not provide directory lookup".
- On Linux and macOS, attachment text indexing and the local semantic-search model now work
  from inside the full-screen app.

### Improved
- `gucmail doctor`, `cache status` and `quota local` print short, readable tables instead of
  raw data. `--json` still gives every detail.
- New keys in the mail list: M marks the selected messages read, E exports them.
- `recurring create` takes `--until 2027-06-30` and `--on mon,wed`.
- Plainer wording: every command group's help line, notification tab names, privacy choices
  and the app title ("gucmail").

### For developers
- The version lives only in `src/gucmail/__init__.py`. Releases happen by pushing a tag;
  PyPI publishing uses trusted publishing, so no token is handled by hand.
- CI runs the whole test suite on Windows and Linux, Python 3.11 and 3.13.
- `python qa/test.py` runs the suite in parallel and cleans up after itself.
- `cli.py` (9,000 lines) is now a `gucmail.cli` package with one module per area.
  `tui/app.py` (13,000 lines) is split into the screens and themed parts of the main window.
- Tests built from the real GUC server's folder layout and behaviour cover the three bugs
  that only appeared on the live account in 1.1.

## 1.1.5 (2026-11-12)

- Toasts no longer say "(No subject)" when your privacy setting only hid the subject.
- New desktop notifications show the sender and subject by default.

## 1.1.4 (2026-11-12)

- Enabling or repairing the background watcher starts it right away.
- The watcher runs without opening a console window.
- A stale lock left by a crashed watcher is recovered automatically.

## 1.1.3 (2026-11-12)

- Profiles without rules now notify about all new mail.
- "default" refers to your active notification profile.

## 1.1.2 (2026-11-11)

- Flagging and categorizing retry once when the message changed on the server.
- Searching all folders skips system folders that can't hold mail.
- Directory search and free/busy work on the GUC server.
- Deleting a draft also removes the server copy.

## 1.1.1 (2026-11-11)

- Easier-to-read CLI output, and cleaner text from HTML mail.
- The folder tree hides folders that can't hold mail.

## 1.1.0 (2026-11-11)

- First public release on PyPI.
