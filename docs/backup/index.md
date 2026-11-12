# Backup, offline browsing, and restore

Backups are immutable, self-describing archives separate from cache and search indexes. A backup
stores a versioned manifest, canonical records, SHA-256 object hashes, content-addressed attachment
objects, checksums, statistics, selection metadata, and parent linkage for incrementals.

## Create and verify

```text
gucmail backup estimate --folder inbox
gucmail backup create --folder inbox --encrypt
gucmail backup incremental BACKUP_ID --encrypted
gucmail backup resume INCOMPLETE_BACKUP_ID
gucmail backup verify BACKUP_ID
gucmail backup inspect BACKUP_ID
```

Encrypted commands prompt for a passphrase. For controlled noninteractive input, use
`--passphrase-stdin`; the passphrase is not stored in the manifest. AES-256-GCM and library KDF
metadata are used; no custom cryptography is implemented.

A mailbox-wide EWS snapshot is not globally atomic. Resumption uses completed item keys and atomic
local manifest transitions. Incrementals rescan the complete saved scope in bounded pages and store
new/changed records plus deletion markers. They do not use native Exchange change tracking.

An incremental inherits its parent's folders, exact selected references, content types and
exclusions. Changing that scope requires a new base backup. In the Backup Center, highlight the
complete archive to continue, choose Incremental and review its ID before creation. Its saved scope
and encryption replace the form's full-backup defaults. The destination must be the same repository;
an encrypted parent's passphrase is needed for estimation as well as creation.

Backups opened from selected messages, search results or a cluster save a fixed set of references.
Their incrementals revisit those references; they do not re-run the query or add new cluster members.
Create another selected backup to capture a new result set. If the local reference map is missing,
the run fails rather than treating unknown references as deletions. A confirmed missing server item
can produce a deletion marker, but incomplete scans and failed body/attachment reads cannot.

New EWS archives bind their chain to a SHA-256 identity derived from the endpoint and actual mailbox.
Continuing or resuming against a different account is rejected. The address is not stored in this
identity field. Older archives without it remain readable and can acquire the binding when continued;
the original account of such an archive cannot be retrospectively verified. Older chains that changed
scope remain browsable but require a new base for further incrementals. Existing manifests are not
rewritten. Keep all ancestors required by an incremental.

## Offline use

```text
gucmail backup browse BACKUP_ID --limit 50
gucmail backup search BACKUP_ID "synthetic phrase" --fuzzy
gucmail backup export BACKUP_ID --output portable.gucmail.zip
```

Browse, search, inspect, verify, and export do not initialize Exchange. Portable export verifies the
chain first. Backup contents remain private even when encrypted metadata minimizes exposure.

## Restore

```text
gucmail backup restore-plan BACKUP_ID --destination "Restored by GUC Mail CLI"
gucmail --yes backup restore BACKUP_ID --destination "Restored by GUC Mail CLI" --apply
gucmail backup restore-status
gucmail backup restore-status OPERATION_ID --limit 100 --offset 0
```

Restore verifies the archive, calculates exact create/duplicate/unsupported counts, and requires
the current plan token. The EWS adapter creates dedicated folders and uses SaveOnly-style creation;
restored messages and drafts are never sent or scheduled. Attachment bytes are re-verified and
hydrated one item at a time under a 100 MiB per-item memory ceiling. Provider failures are surfaced
without automatic retry because a timed-out mutation can be ambiguous.

Ordinary MIME and field-based restores require a usable new item ID and change key before uploading
attachments and before storing a local reference. A lost create response, attachment failure, missing
identity or local-reference failure after creation is reported as uncertain: the item may already
exist. A structured create rejection remains a known failure. Restore stops on the first item error
by default; explicitly continuing processes later items without retrying the failed one. Local
archive-read failures retain earlier successful results and do not claim that the failed item was
created. Inspect the dedicated destination and partial results before confirming another plan.

In the TUI, Ctrl+C requests a stop before the next item starts. The current request may
still finish; keep the screen open to review its result. Reaching the restore time limit
also requests a stop and retains the current result. Stopping a queued restore before its
first request starts shows zero attempts and makes no mailbox change. Escape or Close during an active
restore also requests a stop and keeps the pending result screen open. Completed results
remain visible and the same attempt cannot be submitted again. CLI Ctrl+C during per-item
preparation or creation emits the available partial results before returning exit 130;
interrupted creation is uncertain. Inspect the destination before another plan. These
results do not establish what happened after abrupt process termination.

The normal CLI and Backup Center record restore intent and per-item dispatch in
the account's local state database before calling the provider. A new plan skips
recorded completed items and blocks unconfirmed dispatches, including after
restart. Acknowledgment persistence failure stops the batch and keeps the
dispatch protected even when the current result contains the provider's returned
reference. Known rejected or unstarted targets can be reviewed and retried.
Restore status reads only the local journal and never initializes Exchange.
The TUI's Restore status action opens the same read-only, paged history.

To deliberately create an additional copy of **recorded completed** work, request
`backup restore-plan BACKUP_ID --another-copy`, then use `--another-copy` with
`backup restore ... --apply` and its new confirmation token. In the TUI choose
Another copy, review its fresh plan and confirm Restore separately. This policy
never overrides an unconfirmed dispatch. Encrypted review/execution each require
a fresh passphrase; cancelling leaves the previous plan unchanged.

This is profile-local protection for the exact recorded account, archive chain,
item key and destination string, not server deduplication. Older restores, another
profile, changed chains/destinations or lost local state may not be tracked.
Programmatic BackupService use without a StateStore explicitly reports that no
durable journal is available. Never clear recovery records to force a retry.
Unconfirmed work currently requires destination inspection; automated verified
reconciliation is not implemented. Widget and history pages are bounded, while
large archive restore planning still materializes the selected plan and remains
unqualified for large-workload memory/cancellation.

State schema 16 adds restore records transactionally. Back up state, recovery and
configuration coherently before an upgrade. Older versions reject the new schema;
rollback uses the pre-upgrade copy, not manually lowered schema metadata.

New mail archives include the original MIME as a content-addressed `original-message.eml` object,
marked with the `message_mime` role. Restoration imports that MIME with SaveOnly and does not attach
the separately archived files a second time. Those files remain available for offline extraction.
Older archives use their saved HTML or plain-text body, recipients, Reply-To, categories, importance,
sensitivity, read state, and file attachments. Inline flags, content IDs, and content types survive
that fallback. The MIME object counts as an archive attachment object and contributes to the per-item
memory ceiling; it is not an extra attachment in the restored message.

Calendar backup now pages stored items and recurring masters, without expanding a date-limited
CalendarView. PIM backups include file attachments and contact-photo attachment flags. Old archives
that omitted those bytes cannot recover them. When a calendar, contact, or task contains an embedded
item attachment, a new backup preserves the containing item through one EWS `ExportItems` stream.
Its selected JSON metadata remains available for offline browsing. The opaque stream is stored as
one private, content-addressed archive object; embedded objects are never flattened to files or
recursively opened. Individual embedded attachment contents cannot be extracted from that stream
by the offline browser. Existing file-only PIM archives retain their portable field-based behavior.
Simple recurring calendar and task definitions use the same typed
mapper as creation. Task reminder times are restored. Meeting restoration uses `SendToNone`.

Native streams require a known Exchange version at least 2010 SP1 and positive item-size metadata
within the configured attachment limit. The stored stream is capped at the smaller of that limit
and 100 MiB. Empty, malformed, noncanonical, truncated, multiple, or oversized stream responses fail
the backup explicitly and retain its incomplete directory. These checks apply after the library
materializes its SOAP response; item-size metadata is an estimate, and this is not a hard streaming
HTTP memory bound. Server permission and compatibility have not been live-verified.

The restore plan identifies native stream items before confirmation. `UploadItems` uses `CreateNew`
for one ordinary item in the dedicated destination folder, never a historical ItemId or an update.
A separate metadata-only GetItem verifies the returned type and new identity before local mapping.
Missing identities, bad readback, or local-state failure after acceptance are uncertain outcomes;
the operation is never automatically repeated. The CLI preserves the partial result and exits 10
when any item fails. The TUI reports whether the failed item may already exist and disables replay
of a completed attempt. Inspect the destination before planning another restore. Restore warnings
and controls support keyboard scrolling on compact terminals.

Portable field-based restoration is **best-effort recreation**. Historical EWS IDs, change keys,
delivery timestamps, original organizer identity, permissions, read-receipt history, recurrence
exceptions/deletions, and every extended property are not recreated. Original MIME is preserved
in the archive; Exchange may normalize it on import. Complex recurrence histories still require
additional implementation and verification. Native streams preserve the current containing item's
opaque representation using the EWS export/import mechanism, but do not establish unseen server
history, local Task-family associations, full mailbox consistency, organizer-attendee relationships,
or compatibility with the destination server. They do not turn the archive into a lossless mailbox
clone. See Microsoft's [item export](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/how-to-export-items-by-using-ews-in-exchange)
and [item import](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/how-to-import-items-by-using-ews-in-exchange)
documentation for the separate fidelity and relationship limits. Inspect partial results before repeating a failed
restore: an interrupted CreateItem or attachment upload may have completed remotely.

`backup prune` is preview-first and moves eligible backups to a recoverable local trash directory;
it protects parents needed by retained incrementals.

