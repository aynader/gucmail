# Mailbox quota and local storage

Mailbox storage uses separate values: current server-side usage, Issue Warning, Prohibit Send, and Prohibit Send & Receive. Each number has its own evidence source. GUC Mail never calls the Prohibit Send threshold total capacity and never invents unknown thresholds.

## What the compact indicator means

When current usage and the send-block threshold are both known, the header shows progress toward the sending threshold:

```text
Send quota  ████████░░  78%  72/92 MB
```

If usage is above that threshold, the bar stays visually full but the percentage continues above 100 percent:

```text
Send quota  ██████████  105%  97/92 MB · SEND BLOCKED
```

This means the mailbox contains 105 percent of the amount at which Exchange can block sending. It does not claim that the mailbox is 105 percent of a total physical capacity. Receiving can have a different, still unknown threshold.

When only usage is known, the indicator says `Mailbox 74 MB used · limits unknown` and shows no percentage. When only the send threshold is known, it says `Send limit 92 MB · usage unavailable`. Only when neither value is available does it say `Mailbox storage unavailable`.

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
