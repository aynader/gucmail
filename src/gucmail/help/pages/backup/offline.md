# Browse and search backups offline

Completed backups can be inspected, verified, browsed, searched, and exported without resolving Exchange credentials. The interface should display the backup creation time and source so stale data is not mistaken for a current mailbox.

## Offline boundaries

Offline browsing is read-only. It cannot mark server mail read, accept a meeting, complete a server task, send a draft, or update contacts. Search covers only fields and objects stored in the selected backup.

The offline Calendar, Contacts, and Tasks screens search all saved records of that area in the selected backup, then paginate matching records. Typing resets to the first result page and cancels the previous local scan. Work runs outside the UI event loop; Ctrl+C cancels a scan. Attachment metadata is searchable without opening attachment payloads.

Verify the chain before portable export or restore planning. Encrypted content needs the original passphrase; the passphrase is not stored in the manifest. A missing or mismatched object makes the backup unsuitable for restore until a sound copy is found.

Cache and index can be cleared and rebuilt. A backup is retained intentionally and pruned through a preview that protects parents of kept incrementals. Uninstalling the application does not promise to erase backup directories. See [Local data and privacy](help:privacy.local-data).
