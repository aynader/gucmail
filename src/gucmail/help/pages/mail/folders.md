# Inbox and folders

Exchange mailboxes contain distinguished folders such as Inbox and Sent Items, custom folders, and nested children. Display names can be localized, so the client resolves standard roles rather than assuming English names.

## Browse without loading everything

The folder tree and `gucmail folders tree` use bounded discovery. Expand a branch to load its children. Counts are server metadata and can lag briefly after a change. Refresh once when needed; repeated refreshes can worsen throttling.

Selecting a folder loads one bounded message page. The interactive inbox uses at least the configured page size and, when **Fill the message list** is enabled, loads enough headers to fill the visible list, up to 200. Continue scrolling or move the selection to the last row to load the next page. Turn the setting off to use a fixed page size. The filter and Unread or Flagged views operate on the messages already loaded; use search for a wider scope.

Folder paths preserve their parents when selected directly or through a tree. Escape a literal slash
in a display name as `\/` and a literal backslash as `\\`. For example,
`/Mailbox/Projects/Research\/2026` selects the folder named `Research/2026` below Projects.
Returned paths can be used in another session. A relative role path such as `inbox/Courses` starts
below the distinguished Inbox even when its display name is localized.
Lookup preserves the Unicode characters in existing display names. Creating or renaming a folder
uses the separate new-name validation policy.

## Folder changes

Create, rename, move, delete, and empty operations change Exchange. Review the exact parent path and scope before confirmation. Empty and permanent-delete behavior is higher risk than an ordinary move to Deleted Items. Large or unusual trees should never be flattened into hard-coded English names.

After a successful local rename or move, the client updates retained paths under the known source
path, including descendant reference metadata. It does not enumerate the subtree. Changes made by
another client can leave saved paths stale. A saved reference must still identify the same Exchange
folder; a reused path that identifies another folder is rejected before a mailbox change. Refresh
the folder tree and select the folder again when a conflict is reported.

If a saved path no longer exists, the client can recover the folder by its saved Exchange identity.
It retrieves only the folder's name, class, parent and counts, then follows at most 64 parent
identities to the mailbox root. It does not enumerate the hierarchy or fetch messages. Missing,
ambiguous, incomplete or cyclic metadata stops recovery before a mailbox change. A path that now
identifies a different folder remains a conflict; it cannot redirect the saved reference.

If Exchange accepts a change but local path persistence fails, the error reports that the change may
have occurred and prevents automatic retry. Refresh and inspect the folder before repeating it.
See [Deleted Items and restore](help:mail.deleted-items) for message recovery boundaries.

In Commands, `Open Folder <local-reference>` (or `Open Folder id:<local-reference>`) opens a
saved folder directly. An explicit local reference keeps its exact identity even when the folder
list contains other display names. In Folder properties, Tab focuses the details area for keyboard
scrolling; End reaches the final metadata. Close remains available below the scroll area, and Escape
returns to the mailbox.
