# Layout, preview pane, and message reader

The mail view should use the terminal's remaining vertical space and adapt by proportion rather than fixed pixel-like widths. The exact controls depend on the installed TUI and are listed in its layout menu and active shortcut browser.

## Common arrangements

Wide terminals can show folders, message list, and reader together. Medium terminals may use a two-pane route, while narrow terminals can show one area at a time. Supported layouts can include list only, reader only, horizontal split, vertical split, or a separately opened reader.

When a divider is available, drag it with the mouse or use the displayed keyboard actions. Reset restores a safe default. Collapse should not destroy the saved proportion, and preferences are stored per user without mailbox content.

If the interface is cramped, widen the terminal, reset layout, disable an optional pane, or use command output. Focus and selection should survive a resize. A detached reader is still an inert terminal view; it never loads remote email content. Read the [Interface tour](help:start.interface) and [Shortcuts](help:reference.shortcuts).

## Save and load named layouts

Open **Tools → Saved layouts**, or find **Save layout preset** or **Load layout preset** in the command palette. Enter a name and choose **Save** to keep the current arrangement. Select a saved layout and choose **Load** to apply it. **Remove** deletes the saved entry while keeping the active arrangement. Replacing a name or removing an entry asks for confirmation.

Presets keep pane visibility and proportions, split orientation, reader mode, message columns, and header/status choices. They preserve the current theme, account and mailbox data. Names contain 1–40 printable characters and are unique ignoring letter case; the library holds up to 32 presets. Apply or cancel staged Settings and close Settings before opening saved layouts.

The library uses normal Settings persistence, revision checks, import/export, reset and Undo. A stale preview or failed save leaves the stored configuration intact; refresh after another process changes it. Loading a layout records it as the last chosen preset and restores the arrangement at restart.

For scripts, `gucmail layouts save Reading` saves the configured arrangement. The TUI Save action also captures unsaved session adjustments. `gucmail layouts load Reading --dry-run` previews choices; omit `--dry-run` to apply them. Use `--replace` to replace a name and global `--yes` before `layouts delete Reading` for confirmed automation. All layout commands operate locally.

## Choose message columns

Open Settings, search for **Message columns**, and choose **Edit columns**. Select a column to change its visibility, compact-list visibility, or width. Leave width blank for automatic sizing. Use Up/Down or Alt+Up/Down to reorder it. Hidden columns retain their order and width.

Choose **Stage columns**, then **Preview** in Settings to inspect the actual mailbox-list columns using up to 200 already loaded rows. No mailbox request or configuration write is made. **Cancel preview** restores the exact previous columns and discards staged changes. **Keep editing** returns to Settings with the temporary preview active; another preview starts from the original state. Closing Settings also restores an unapplied preview.

**Apply staged settings** keeps all staged preferences through the normal Settings Apply workflow. You can also choose Apply directly in Settings. Defaults in the editor restores the adaptive column choices. Settings reset, history, import/export and restart preserve the same preference. The default shows state, sender and subject, adding received date and size when the message list has enough room.

Optional columns show attachments, flags, importance, sent date, folder and conversation metadata. These use the loaded message summaries. Fixed widths can require horizontal scrolling; keep at least one visible compact column so a narrow list stays usable. Column choices do not change the server query or fetch bodies.

## Header and status-bar contents

In **Settings → Layout**, edit **Header contents** as a comma-separated list of `account`, `storage`, `monitoring`, and `folder`, in your preferred order. Removing `storage` or `monitoring` hides that indicator and its tooltip facts. An empty list leaves the brand and command launcher visible. These choices use existing status facts and never fetch mailbox data.

**Header compactness** keeps the header one row high. **Adaptive** drops folder, monitoring, then storage text as space shrinks. **Compact** starts with a short “Storage details” label; the selected facts remain available in the tooltip and Storage workspace. **Detailed** keeps the chosen fields together and uses an ellipsis when needed. A compact label does not imply a known quota or percentage.

**Status-bar contents** accepts `activity`, followed by optional `connection`, `folder`, and `monitoring` fields. `activity` must stay first so loading, failure, and operation results remain visible. Status-bar visibility is a separate preference. **Stage**, then **Apply** to save these settings; staging or closing without Apply leaves the current display unchanged. Reset, Undo, import/export, and restart use the same settings system.

If a divider or layout shortcut cannot save its preference because of an override or a configuration write failure, the change remains available for that session and a warning identifies that it was not saved.
