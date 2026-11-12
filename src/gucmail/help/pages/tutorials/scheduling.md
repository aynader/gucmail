# Tutorial: schedule a message without sending

For queue-free practice, choose **Tutorials** → **Schedule a message** in Help. It resolves a synthetic delivery time with the application's date parser, shows the absolute time and offset, and checks the worker requirements. It cannot create a draft or scheduled job. You can close, resume or restart the lesson at any time.

## Real scheduling workflow

These steps create a real eligible job. Use a recipient you intend to contact; do not queue a message to an example address for practice. A running worker can send a confirmed job as soon as it becomes due.

1. Create a local draft addressed to the intended recipient and review its content.
2. List drafts and copy its local reference.
3. Preview a schedule such as tomorrow at 10:00 in the configured time zone.
4. Confirm the displayed absolute date, UTC offset, and local zone.
5. List the scheduled job and inspect its attachment and confirmation state.
6. If you intend to cancel, do so before its due time and verify the cancellation state. History remains while the job is no longer eligible to send.

Avoid ambiguous numeric dates such as `03/04/2027`; use an ISO date or explicit words and review the result. Cancellation does not erase audit history or delete the underlying draft, and it cannot retract a message already accepted by Exchange. When relying on real scheduling, install or supervise a one-shot worker and read [Scheduled mail](help:mail.scheduled) plus [PC-on behavior](help:faq.pc-on).
