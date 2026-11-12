# Notification filtering, quiet hours, and digests

Exchange mailbox policies are available separately under **Notifications → Exchange
inbox rules** or **Manage Exchange Inbox Rules** in the command palette. Inspect
conditions, exceptions and actions; create simple rules disabled; review and confirm
enabling, disabling or deleting an exact rule. `inbox-rules --help` lists scriptable
commands. Fresh policy checks reject stale previews, and Outlook's client-side rule
data is preserved. Account support remains unprobed.

Notification decisions should be explicit, ordered, and testable. A rule may use sender, recipient, domain, subject, folder, importance, attachment presence, time window, category, or bounded text fields when the local event contains them.

## Safe rule design

Use grouped AND, OR, and NOT conditions instead of ambiguous prose. Preview a rule against synthetic events or a bounded local event sample. The preview explains which condition matched, which provider would receive the event, and what privacy transformation applies.

Open Notifications, select Rules, and choose New or Edit. Select a condition in the tree to add or
edit it, wrap it in AND/OR, or toggle NOT. Duplicate, move, and remove controls apply to children
within a group. Typed forms cover text comparisons, booleans, choices, sizes, local time windows,
and weekdays. Alternative text values use one line each. Actions appear in execution order;
edit their destinations and use Up/Down to reorder them. New rules start disabled. Advanced JSON
is available in a collapsed section; Apply JSON refreshes the guided controls after validation.

Use **Simulate** for synthetic input, or **Simulate recent mail (read-only)** for a bounded sample
of up to 50 message summaries across at most ten configured folders. Neither sends notifications.
The recent sample contains no bodies, recipient lists, or attachment filenames; conditions needing
those fields do not match by guessing. Empty results remain empty. The report states its limits
and shows proposed routing and disclosed fields. Recent-mail sampling is unavailable offline.

The preview applies the watcher's routing matrix, enabled-provider checks, provider filters,
quiet hours and privacy policies. The selected rule is evaluated even if disabled, without
enabling it in saved settings; this allows testing before activation. Planned counts include
queued and digest events. Matching
examples identify the message reference, folder, subject, rule and destination; suppressed or
skipped routes are shown. This preview does not start monitoring or promise successful delivery.
Tab reveals the focused action; focus the scrollable pane to read long reports with Page Up/Down,
Home and End. Action rows adapt when the terminal is resized.

Use `gucmail notifications rules test RULE --profile PROFILE` for one synthetic rule
preview. Add `--recent --limit 50 --max-folders 10` to deliberately sample recent mailbox metadata.
Omit RULE to preview all profile rules. Synthetic mode never resolves mailbox credentials;
recent mode performs only the bounded summary requests and leaves watcher checkpoints unchanged.

The Routing Matrix tab lists each rule/provider pair. Cycle a destination through Off, Immediate,
and Digest, or disable it explicitly. Saving a route changes future configured watcher behavior;
simulation remains a dry run.

In Providers, choose Edit to change privacy, burst limits, or an additional provider filter.
The filter narrows the events that already matched a rule and were routed to this destination.
Edit filter opens the same condition tree and typed forms used by rules, without delivery
actions. Use filter returns to the provider form; Save provider applies the change. Cancel
either form discards its edits. Remove filter explicitly allows every event routed to the
provider; review that broader scope before saving. Renaming a provider preserves its filter.
Non-secret metadata JSON is available in the collapsed Advanced section. Secure destinations
and credentials use the separate provider setup flow.

## Exchange automatic replies

Automatic replies are a separate mailbox policy. Open **Automatic replies** from the Notifications
overview, or use **Manage Automatic Replies** in the command palette. Opening it reads the current
policy without enabling it. Choose Edit policy, set Disabled, Enabled, or Scheduled, select the
external audience, and enter the internal and external messages. Scheduled times require an
explicit UTC offset. Review, then confirm Apply policy. Escape cancels either step.

The current policy is checked again before applying. If another client changed it, refresh and
review a new preview. A failed or mismatched readback is reported as unverified after the accepted
write; the client never repeats that write automatically. Account permissions may deny this
feature. Offline mode cannot change it.

For automation, use `gucmail automatic-replies show`, or `automatic-replies set --state STATE`
with optional UTF-8 reply files, external audience and scheduled times. Use `--dry-run` to inspect
the exact policy before applying; noninteractive mutation requires `--yes`.

Quiet hours can suppress, queue for a digest, or allow high-priority exceptions. A digest combines only the configured fields and must not bypass provider privacy settings. Profiles can group rules and routes for study, work, presentation, or privacy modes.

Rules are local automation. They do not create Exchange inbox rules unless a separate capability-gated mailbox-rule action explicitly says so. A malformed or unavailable provider fails closed and leaves a delivery-history entry. See [Windows notifications](help:notifications.windows) and [Generic webhooks](help:integrations.webhooks).
