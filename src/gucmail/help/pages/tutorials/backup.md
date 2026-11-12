# Tutorial: plan and verify a first backup

Use estimate and inspection before committing storage. This tutorial does not restore anything and should not alter Exchange items.

## Plan

1. Run `gucmail backup estimate --folder inbox` and review item, attachment, and byte estimates.
2. Choose a private destination with enough free space.
3. Allow space for the selected items and their attachments; decide whether encryption is needed.
4. If encrypting, enter the passphrase through the secure prompt or controlled standard input, never a command argument.

## Create and verify

Start the bounded backup. If interrupted, retain the incomplete directory and use resume rather than starting overlapping copies. When completion is reported, run verify and inspect the manifest summary. Keep the passphrase separately and test backup browsing before relying on the copy.

To continue it later, highlight that completed archive in the Backup Center and choose Incremental, or run `gucmail backup incremental BACKUP_ID`. The saved scope is inherited, including any fixed search-result references. Review the parent ID before confirming. Keep its ancestors; a delta alone is not a standalone backup. Create a new base to change scope.

Do not practice restore against a live mailbox. Restore planning is local and read-only; actual restore creates server items and requires a separate exact plan token and confirmation. Read [Backup overview](help:backup.overview) for format and consistency limitations.
