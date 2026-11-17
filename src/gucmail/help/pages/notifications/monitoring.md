# New-mail alerts

Monitoring is disabled until deliberately started. A quiet first run records a baseline rather than announcing every existing message. Later cycles compare a persisted checkpoint and stable event identifiers.

## Detection strategy

Default mail monitoring uses bounded timestamp polling. Choose `watch --strategy incremental_sync` for native Exchange creation, update, read-state and deletion changes. This reads one metadata-only page per folder each cycle. No bodies or attachment payloads are downloaded. Pull, streaming and push subscriptions are not implemented.

Native sync first establishes a quiet baseline across the existing folder metadata. Each page is saved so restart continues at the next page. New arrivals during initialization may join the quiet baseline. `more_available` means more pages remain; subsequent normal-interval cycles continue them. `--notify-existing` opts into announcing baseline items. A failed page retains the previous cursor. An expired cursor requires an explicit `--reset-checkpoint` on the same command and strategy.

`calendar watch` uses native sync to report stored event/series creations, updates and deletions without outbound delivery. Add `--forever` for continuous operation. It does not expand occurrence reminders. For explicitly configured delivery destinations use `watch --strategy incremental_sync --folder calendar`.

One authenticated session is reused. A safe minimum interval prevents rapid polling. Network loss, throttling, sleep, and server backoff extend the interval with bounded exponential backoff and jitter. Duplicate events are filtered transactionally across overlapping processes.

## Operation

Foreground watch stops cleanly on Ctrl+C. One-shot operation is suitable for an OS supervisor. Checkpoint status, last successful cycle, next attempt, active strategy, and recent delivery outcomes belong in Activity and Watch Doctor. Notification providers receive only configured allowlisted fields. See [PC-on behavior](help:faq.pc-on) and [Webhook privacy](help:integrations.webhooks).
