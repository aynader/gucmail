# Read messages safely

Message summaries and full message detail are intentionally separate. A folder page asks Exchange for a small field set; the reader fetches the selected body and metadata only when requested.

## Choose folder order

Open **Settings → Mail** and choose **Message sort field** and **Message sort direction**.
Received date, sent date, subject, sender, size and importance are available. The default is
received date descending. **Stage** leaves the folder unchanged; **Cancel** discards staged
choices. **Apply** saves them for this account and refreshes a bounded folder page when connected.
Selection stays on the same message when it is still in the new page.

The preference survives restart and supports Settings Reset, Undo and import/export. Folder
ordering is independent of explicit search ranking and does not require loading the whole
mailbox. If Exchange rejects an order, the error is shown without an unannounced local fallback.

`gucmail messages list` uses the same preference. An explicit `--order-by=-size` overrides it
for one invocation. Use `--limit` (up to 200) and `--offset` for bounded pages. Available explicit
fields include `datetime_received`, `datetime_sent`, `subject`, `sender`, `size` and `importance`;
a leading minus requests descending order.

## Group conversations in a bounded page

In **Settings → Mail → Group conversations within each page**, enable grouping to place rows
with the same exact Exchange conversation ID together. The default is off. Stage and Cancel
leave the current view unchanged; Apply changes its presentation without fetching more mail.
Reset, Undo, settings transfer and restart preserve the account's canonical choice.

Every message retains its own row, reference and action target. A `[1/2]` subject prefix counts
only matching members in the visible page; it does not imply the whole conversation was loaded.
Missing IDs and similar subjects are not grouped. Selection is preserved when its reference is
still visible, and turning grouping off restores the original page order. Filters and pagination
stay bounded. Grouping does not change read state or select hidden mail; CLI list order is unchanged.

## Choose the ordinary Delete destination

**Settings → Mail → Ordinary Delete destination** defaults to `deleted_items`. Choose
`move_to_folder` and enter **Delete destination folder** to move mail to an existing folder of
your choice. No folder is auto-created. A missing destination produces an error before connecting.

The **D** shortcut reviews exactly one highlighted message and its captured destination. Cancel
leaves it unchanged. Changing selection or preferences during review does not change the target.
Permanent deletion remains a separate explicit action and cannot become a saved default.

`gucmail messages delete MESSAGE_REF --dry-run` shows a local plan without credentials or a
connection. `--to FOLDER` overrides the saved folder, and `--behavior deleted_items` overrides a
custom destination. Global `--yes` approves automation. `--permanent` cannot be combined with a
destination override. After an uncertain provider outcome, refresh before attempting another move.

## Reader behavior

The reader converts HTML into inert text. It does not execute scripts, submit forms, load frames, fetch tracking pixels, or automatically retrieve remote images. Terminal control characters from subjects, names, headers, filenames, and bodies are removed before rendering.

Opening a message leaves its read state unchanged by default. Use the explicit read/unread action when that change is desired. Attachment listing retrieves metadata first and does not download every file.

In Settings → Mail, **Mark read on open** enables automatic read-state changes. **Mark read after seconds** accepts 0–300 seconds; zero starts after the message is displayed. The delay starts only for a live, unread message in the preview, full-screen, or floating reader. The same message must remain selected and visible when the timer and worker start. Changing selection, closing or covering the reader, changing folders, cancelling a load, going offline, or changing this preference abandons the pending change. Reader-mode choices, advanced details, external exports, cached data, and backups do not start it.

A read-state request already sent to Exchange can finish after leaving the reader. Cancellation does not reverse that change. A failed automatic update is not retried automatically; refresh to inspect the server state. Changed timing preferences apply to the next opening.

Advanced details can include headers, Internet Message ID, conversation identifiers, Exchange item ID and change key, categories, sensitivity, and safe extended properties when available. Raw MIME or headers are capability-gated and should not be treated as a complete forensic record.

Use message references from list output instead of typing long Exchange identifiers. After a move or update changes the server identity, refresh the containing folder if a reference becomes stale. See [Attachment safety](help:attachments.safety).
