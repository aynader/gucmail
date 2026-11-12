# Monitoring and notifications

Optional [Windows desktop setup](../integrations/windows-desktop.md) configures an owned
notification identity and read-only opening path. Native visible display and activation
still require Windows verification; setup does not start the watcher.

Monitoring is disabled until the user starts it. A quiet first run records a baseline instead of
notifying every existing message.

```text
gucmail watch --folder inbox
gucmail watch --folder inbox --forever
gucmail watch --folder inbox --desktop --event-log notifications.ndjson
gucmail --yes watch --folder inbox --command-hook C:\Tools\mail-event.exe
gucmail notifications test
gucmail notifications history
```

The default mail watcher uses bounded timestamp polling with backoff. Native Exchange change
synchronization is available explicitly:

```text
gucmail watch --strategy incremental_sync --folder inbox --no-webhooks
gucmail --ndjson watch --strategy incremental_sync --folder inbox --forever
gucmail --json calendar watch
gucmail --ndjson calendar watch --forever
```

Native sync reads one metadata-only page per folder per cycle, using the configured page size.
It detects creations, updates, read-state changes, and deletions. Moves between folders may appear
as a deletion and creation; these are not falsely correlated into a single move event. It downloads
no bodies, attachment payloads, or calendar event times. Calendar monitoring watches stored events
and series changes, not expanded occurrence reminders.

The first native sync gradually walks the selected folder's existing metadata. All baseline pages
are quiet by default, including after restart; `--notify-existing` explicitly announces them.
New arrivals during this initial baseline may be included in that quiet baseline. Each completed
page commits its cursor and detected events atomically; `more_available` reports remaining pages.
Backlogs progress at the normal interval rather than draining the whole folder in a burst. A
one-shot invocation performs only one native page, so repeat it through a supervisor to finish
initialization. Once initialized, the opaque Exchange cursor supplies subsequent changes.

An invalid or expired cursor stops that folder's sync without discarding progress. Use the same
command and strategy with `--reset-checkpoint` to deliberately build a new quiet baseline. Other
folders' checkpoints and notification history remain intact. Ordinary polling checkpoints remain
separate from explicitly selected native-sync checkpoints.

There is no active pull, streaming or push subscription transport. The active status names the
actual polling/sync method. Foreground mode stops on Ctrl+C; structured output emits each cycle,
and human console notifications go to stderr when JSON/NDJSON is selected. Failed one-shot mail
cycles return exit code 10. Native sync and its initial bounded pages were verified read-only on
GUC; restart, replay, expiry and deletion behavior use fixtures.

Provider delivery is claimed transactionally in SQLite before a hook runs. This prevents two
overlapping watcher processes from delivering the same event/provider pair at the same time. A
crashed process leaves a time-limited lease that can be recovered. Successful deliveries remain
deduplicated; failed attempts may be retried.

`notifications history` reads the newest append-only local outcomes. Each row includes the event
reference, provider, state, attempt count, optional HTTP status, redacted detail, and time. Failed
and later successful attempts remain separate rows. The ledger retains the newest 10,000 outcomes;
the command displays 50 by default and accepts `--limit` up to 500. In the TUI, **Activity** shows
whether monitoring is configured, recent checkpoint status, and the latest notification outcomes.

The console provider is active by default. `--desktop` requests the optional desktop provider and
`--event-log` appends redacted structured events to a local NDJSON file. Command hooks are absent by
default; supplying `--command-hook` requires confirmation and invokes the executable directly with
no command shell. Use repeated `--command-arg` options for fixed arguments.

Definitions created with `webhooks add --enable` (or later enabled with `webhooks enable`) are also
loaded by `watch`. Use `--no-webhooks` for a run that must remain local. `calendar watch` reports
structured calendar changes without outbound delivery. To route calendar changes through the
configured notification providers, explicitly use `watch --strategy incremental_sync --folder
calendar`. Provider-free monitoring never consumes another watcher's pending deliveries.

The native wire boundary follows Microsoft's [SyncFolderItems reference](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/syncfolderitems-operation).

See [Integrations](../integrations/index.md) for signed webhooks and [Scheduling](../scheduling/index.md)
for the separate scheduled-send worker.

For a persistent TUI-free process, follow [Headless watcher deployment](headless-deployment.md).

## Rule previews

Open **Notifications → Rules** to edit conditions and ordered actions through guided forms.
**Simulate** uses synthetic metadata; **Simulate recent mail (read-only)** requests at most 50
message summaries across ten profile folders. Neither operation starts monitoring, delivers a
notification, fetches bodies or attachment content, or advances watcher checkpoints. Rules needing
recipient lists, bodies or attachment filenames remain unknown with this metadata sample.

For automation, an optional rule ID restricts the preview within the named profile:

```text
gucmail --json notifications rules test exams --profile campus
gucmail --json --non-interactive notifications rules test exams --profile campus --recent --limit 50 --max-folders 10
```

Omit the rule ID to preview all profile rules. Synthetic preview is the default and needs no
mailbox access. An explicitly selected rule is evaluated even when disabled so it can be tested
before enabling; the saved rule is never changed. All-profile previews honor saved enabled states.
`--recent` deliberately resolves the configured account and reads bounded metadata;
invalid rule IDs and limits fail before that access. Recent output includes matching message
references, subjects, routes and exposed field names, so keep it private like other mailbox output.

Profile previews use the same router as the watcher, including the routing matrix, enabled
providers, provider filters, quiet hours and privacy policy. Counts describe planned events,
including queued or digest routes; they do not promise provider delivery, available credentials,
burst-budget acceptance or an active watcher. Examples identify skipped providers and suppression.
The TUI panes scroll with keyboard focus and resize their action rows so controls remain visible
in smaller terminals. Focus the pane to use Page Up/Down, Home and End for long reports.

In **Notifications → Providers → Edit**, an additional provider filter narrows events
that already matched a rule and were routed to that destination. **Edit filter** opens
the shared condition tree and typed forms; it has no delivery actions. **Use filter**
returns edits to the provider form, and **Save provider** applies them. Cancelling either
form discards its edits. **Remove filter** allows every event routed to that provider;
review that broader scope before saving. Renaming a provider retains its filter.
Optional non-secret metadata JSON is in the collapsed Advanced section. Secure destinations
and credentials continue through the separate provider setup flow.
