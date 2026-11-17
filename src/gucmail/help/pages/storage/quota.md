# Mailbox storage

Mailbox storage uses separate values: current server-side usage, Issue Warning, Prohibit Send, and Prohibit Send & Receive. Each number has its own evidence source. GUC Mail never calls the Prohibit Send threshold total capacity and never invents unknown thresholds.

## What the header shows

When usage and the send limit are both known, the header compares them:

```text
Mailbox 74 MiB of 92 MiB (81%)
```

The percentage can go above 100%. That means sending is blocked, not that the mailbox is over a total capacity. Receiving can have a different, unknown limit.

When only usage is known it says `Mailbox 74 MiB used`. When only the limit is known it says `Mailbox limit 92 MiB`. When neither is known it says `Storage unavailable`.

Usage is the sum of your folder sizes as the GUC server reports them. Hidden system folders add a few MiB that this does not count. gucmail checks again every 10 minutes.

## Threshold evidence

The detail view records a source for every value:

- **Server reported** — authenticated account-specific information returned by Exchange.
- **Server notification detected** — a value extracted reliably from an Exchange quota notice.
- **Automatically detected** — derived from another supported server property.
- **User configured** — an explicit override, shown alongside any detected value.
- **Known GUC fallback** — the configurable 92 MB Prohibit Send observation.
- **Unknown** — no defensible value exists.

The default fallback model is Issue Warning unknown, Prohibit Send 92 MB, and Prohibit Send & Receive unknown. A historical warning showed approximately 97 MB in the mailbox while sending was blocked at 92 MB. That proves 92 MB is a send threshold, not total capacity. A previous generic 100 MB assumption is not treated as verified server data.

## Current usage and estimates

Exact server-reported mailbox statistics are preferred. If Exchange does not expose them to the account, GUC Mail can estimate usage from bounded server-side item-size metadata. Estimation does not download bodies or attachments merely to count bytes, avoids double-counting virtual results, and includes visible Deleted Items where possible.

An estimate is always labelled **Estimated**. Hidden or recoverable-deleted data may be absent, so the Storage screen explains its coverage and observation time. Folder analysis can identify large folders and messages without pretending the sum is an exact Exchange statistic.

The Storage screen keeps server mailbox usage separate from application state, disposable cache, full-text index, downloads, recovery journals, logs, and backup archives. Clearing cache cannot remove schedules, drafts, indexes, or backups.

## Send safety and cleanup

Before transmission, a verified send-blocked state prevents sending and links to Storage. A near-threshold state warns without fabricating a block. Refresh is bounded and respects the configured cooldown; opening the screen should not enumerate the entire mailbox repeatedly.

To free server space, analyze by folder, message size, attachment size, age, sender, and duplicate evidence. Create a preview-first [cleanup plan](help:cleanup.overview), prefer ordinary deletion into Deleted Items, and remember that Deleted Items can still consume server storage until emptied. Permanent deletion remains a separate high-risk action.

If storage is unavailable, use Refresh, Detect from mailbox, Configure thresholds, or Run diagnostics. Doctor reports retrieval path, evidence source, last attempt, safe error class, and whether retry is appropriate without exposing mailbox content.
