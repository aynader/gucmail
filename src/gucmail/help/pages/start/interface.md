# Interface tour

The terminal interface presents mailbox information without requiring a mouse. The active theme and terminal width determine the exact arrangement, but the same service layer handles actions from the TUI and command line.

## Main areas

For optional keyboard practice, open Help and choose **Tutorials** → **Find your way around**. It uses a synthetic message list, saves your current step and can be restarted without contacting Exchange. All seven lessons are also available through **Guided Tutorials** in All Commands.

- The folder tree selects standard, custom, and nested folders.
- The message list shows bounded summaries and maintains the current selection where possible.
- The reader fetches message detail on demand and blocks active or remote HTML content.
- The status area explains loading, offline, error, and capability states.
- The footer shows shortcuts that apply to the focused screen.

Use Tab and Shift+Tab to change focus, arrows and paging keys to move, Enter to open, Escape to go back, `?` for contextual Help, and Ctrl+K for the command palette. Available layout controls are shown in the layout menu or shortcut browser; the application should not invent a control that your installed build does not expose.

## Narrow terminals

The interface may route to one pane at a time. Widen the window when practical, or use the scriptable commands for exact output. See [Layout and message reader](help:appearance.layout) and [Keyboard shortcuts](help:reference.shortcuts).

## Returning to your workspace

After a normal close, the TUI can restore your last mail pane and a bounded position in
its first message page. It also remembers a visible folder-tree scroll position. The
last folder and layout use your existing preferences. A hidden pane falls back to the
message list, and positions adjust to the rows and terminal size available on restart.
A restored row does not open a message or mark it read.

Local Search remembers a source choice among all local sources, the full-text index,
indexed attachments, and backup archives. Reopening it starts with an empty query and
waits for you to search. Send/delete confirmations and pending actions never reopen.
Unsent message recovery remains in [Drafts](help:mail.drafts).

If local state cannot be saved, or another app saved a newer session, the previous position
is preserved. A forced process termination may lose your last position.


Mail Search also remembers the last live scope: current folder, all mail folders, or a selected
folder. **Search Mail** reopens an empty prompt with that scope after restart; it does not run the
previous query. Explicit Current Folder/All Mail commands override the remembered choice.
Changing the default search source in Settings supersedes the old session choice. Local Search
retains its separate local source choice. Earlier session rows remain readable and gain the new
safe fields only when a valid session change is saved; existing data is preserved.
