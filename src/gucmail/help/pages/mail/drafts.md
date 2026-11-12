# Drafts, autosave, and recovery

A local recovery journal, durable local draft, Exchange server draft, synchronized draft, and conflicted draft are different states. The interface should always identify which copy you are viewing.

## Autosave and revisions

While composition is open, recovery data is written atomically at the configured interval and once more when a safe cancellation needs preservation. Autosave never sends. Durable saves use revision checks so an older editor cannot silently overwrite a newer copy.

Use `gucmail drafts list`, `show`, and `recover` to inspect local state. A changed attachment is recorded in the manifest and blocks sending until reviewed. Server synchronization is bounded and conflict-aware; it must not save unrestricted fields from a partially fetched Exchange object.

## After a crash

Open recovery, compare timestamps and content hashes, then choose which copy to preserve. Do not delete the recovery directory until important drafts have been reviewed. If a server mutation timed out, the client should retain a manual-review state instead of assuming success. Continue with [Scheduled mail and Outbox](help:mail.scheduled) when a saved draft needs a future send.

## Check whether a draft was sent

Use `gucmail drafts send-status dft-example` with your draft's local reference, or choose **Inspect Draft Send Outcome** in the command palette. This reads the local send record without contacting Exchange.

`sent` means the service recorded Exchange accepting the request. `sending` or `uncertain` means acceptance could not be conclusively recorded; the client blocks resubmission of that draft across restarts. Check Sent Items and delivery before intentionally duplicating a draft for another send. `failed` records a definite rejection and permits another explicitly confirmed attempt. `no_recorded_attempt` is not proof that a message was never sent by an older client or another application.

A successful send removes the matching saved local revision. An independently edited newer revision is retained. Autosave never clears the send record or turns an uncertain send into a retry.

Changing composer defaults affects new compositions. A saved or recovered draft retains its own body format, importance, signature, receipt and Sent-copy choices. Scheduled and Undo Send drafts preserve those reviewed fields too. Changing the Sent-copy choice after scheduling requires an explicit job review. A server-only draft import starts with copying enabled; existing local choices survive server content merges and conflict resolution. An approved empty-subject warning is stored with the queued job, so changing the default later does not silently change that review.

The composer and `drafts edit --save-sent-copy` / `--no-sent-copy` can change this local send
policy. Legacy drafts, recovery envelopes and queued jobs keep their historical copying
behavior. When copying was disabled, an uncertain outcome still blocks automatic retry;
establish delivery manually before intentionally duplicating the draft.

## Save attachments safely

The Drafts & Attachments workspace lists bounded metadata before downloading content.
In Settings → Mail, **Attachment collision behavior** saves the account default: rename,
error, or overwrite. The workspace control overrides that default for the current download.
Both CLI attachment save commands accept `--collision` for the same one-time override.

Saving all prepares exact paths before downloading. Rename assigns distinct names to
duplicate normalized filenames. Error and overwrite reject duplicate destinations within
the batch; choose rename or save individual files under distinct names. Overwriting existing
files still requires a dialog showing the exact paths, or global `--yes` after CLI `--dry-run`.
A file created after preview is preserved rather than silently overwritten. A failed batch
can leave earlier completed downloads; it does not roll them back. Files are never opened
or executed automatically, and the download size and path safeguards still apply.
