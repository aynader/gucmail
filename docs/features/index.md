# Mail and local features

## Quick explanation

The default interface keeps ordinary mail actions visible and moves Exchange-specific details into
advanced or expert views. Features are implemented through shared services, so the TUI and command
interface use the same safety checks.

What works for you also depends on the server and your account permissions.
`gucmail ews capabilities` lists what the client implements; its server and account fields show
`not_probed` until a capability has been checked against your server.

## Everyday mail

Use Inbox or another folder to list a bounded page of message summaries. A message body and its
attachments are fetched only when requested. The reader converts HTML to safe text and does not
load scripts, frames, remote images, or tracking pixels.

Typical commands are:

```text
gucmail folders tree
gucmail messages list --folder inbox --limit 25
gucmail messages show MESSAGE_REFERENCE
gucmail attachments list MESSAGE_REFERENCE
```

Message references are local, stable handles. They avoid copying a long Exchange item ID. A move or
server update can change the underlying Exchange ID; the local reference mapping is updated when
the operation returns a replacement.

## Compose, drafts, and delivery

Composition validates recipients, empty subject/body, attachment paths and sizes, duplicate
recipients, and common missing-attachment wording. Sending always has a review point. Reply all
must show its resolved recipient list because aliases and hidden recipients can make automatic
recipient selection surprising.

The draft service can write a local recovery copy; it never sends a message. A server draft, local
recovery draft, synchronized draft, and conflicted draft are distinct states. See the
[user guide](../user-guide/index.md) and [scheduled-message guide](../scheduling/index.md).

## Folder and message changes

Marking read/unread, changing categories or importance, moving, copying, and ordinary deletion are
**mutating** server operations. Ordinary delete moves an item to Deleted Items where possible.
Permanent deletion is **destructive**, requires a separate explicit irreversible option and
confirmation, and is never the default for a bulk operation.

Folders are selected by user-facing paths and may have localized names. The implementation resolves
standard folder roles instead of assuming English display names. Large trees and message lists are
loaded in bounded pages.

## Storage, insights, and cleanup

`gucmail quota show` keeps Exchange's Issue Warning, Prohibit Send, and Prohibit Send & Receive
thresholds independent. It labels exact, estimated, notification-derived, configured, fallback, and
unknown values. The known 92 MB GUC value is a send-block threshold, not total capacity. See
[Mailbox storage and Exchange thresholds](storage-and-quota.md). Local cache, index, and backup
sizes are reported separately.

Insights can summarize bounded storage metadata by folder or sender and find exact duplicate
candidates. Conversation-complete unanswered-mail analysis is explicitly unavailable because the
adapter cannot yet prove aliases and full thread membership. Cleanup is plan-based:

1. Analyze a bounded scope.
2. Review candidates, exact counts, exclusions, and estimated reclaimed space.
3. Export or inspect the plan.
4. Confirm a recoverable action.
5. Review the result and recovery guidance.

Cleanup never permanently deletes by default.

## Local-only features

Saved searches, durable drafts, schedules, webhook definitions, checkpoints, and deterministic
search clusters are local. The database schema reserves metadata for future pins, snoozes,
reminders, notes, and templates, but this release does not expose those as finished workflows.
Removing local state does not delete server mail, but it can destroy scheduled jobs, recovery
metadata, and integration history; follow the retention guide instead of deleting it casually.

## Progressive disclosure

- **Basic:** mail, attachments, search, drafts, scheduling, calendar, contacts, and simple quota.
- **Advanced:** folders, detailed filters, backup, monitoring, rules, local indexing, cleanup,
  metadata, and structured exports.
- **Expert:** capability inspection, typed SOAP operations, extended properties, sync states, and
  sanitized diagnostics. Arbitrary raw-SOAP execution is intentionally unavailable.

Changing mode changes presentation only. It does not change mailbox semantics or permissions.

## Troubleshooting

- A command says **unsupported**: inspect `gucmail ews describe OPERATION`; the server version or
  account may not allow it.
- A reference is stale: list the folder again so the local mapping can be refreshed.
- A bulk operation stops partway: review the per-item result. Exit code `10` means partial success.
- Storage values are partial: inspect each value's source; never treat a folder-size estimate or the
  92 MB send threshold as a total mailbox capacity.

See [Troubleshooting by symptom](../troubleshooting/index.md).

## Security and privacy

Email fields and filenames are hostile input. Terminal control characters are removed, attachment
paths are contained beneath the chosen directory, and active HTML is not rendered. Saving an
attachment or enabling a command hook requires an explicit user action. See
[Privacy and security](../privacy-and-security/index.md).
