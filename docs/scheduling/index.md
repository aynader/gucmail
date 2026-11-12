# Scheduled messages, delayed sending, and Undo Send

## Quick explanation

GUC Mail CLI uses a durable **local scheduler** by default. The draft and job are stored locally;
Exchange receives the message only when a scheduler process claims the due job and sends it. The TUI
does not have to stay open, but the computer must be awake, online, and able to unlock the account
credential when the job runs.

The capability registry includes Exchange's deferred-send extended property as `partial` and
`not_probed`. It is not selected merely because the property exists. Native server scheduling may
be used only after its semantics are conservatively verified on the detected server.

## Draft, scheduled, outbox, and sent

- A **draft** is editable content that has not been sent.
- A **scheduled message** is a draft snapshot plus a future absolute instant.
- The **outbox** is the local queue and its history.
- A **sent message** is one whose server outcome has been confirmed.

The queue uses these states: `draft`, `scheduled`, `undo_delay`, `due`,
`waiting_connectivity`, `waiting_authentication`, `sending`, `sent`, `failed`,
`retry_scheduled`, `cancelled`, `send_state_uncertain`, and `manual_review_required`.
A queued message never silently disappears.

## Schedule a draft

Use an absolute time with an explicit offset when possible:

```text
gucmail drafts schedule DRAFT_REFERENCE --at "2026-08-06T09:00:00+03:00"
gucmail scheduled list
gucmail scheduled show SCHEDULE_REFERENCE
```

Relative expressions are normalized before storage:

```text
gucmail send --to "recipient@example.test" --subject "Example" --body "Example body" --in "45 minutes"
gucmail drafts schedule DRAFT_REFERENCE --at "tomorrow 10:00"
```

Before confirmation, check the displayed absolute date, time, offset, and zone. An input such as
`03/04/2027` is ambiguous and must not be guessed. The configured default zone is `Africa/Cairo`.
Schedules are stored as unambiguous timestamps; the original expression may remain in history for
explanation.

## Edit, reschedule, cancel, or send now

```text
gucmail scheduled edit SCHEDULE_REFERENCE
gucmail scheduled reschedule SCHEDULE_REFERENCE --at "2026-08-10T09:30:00+03:00"
gucmail scheduled cancel SCHEDULE_REFERENCE
gucmail scheduled send-now SCHEDULE_REFERENCE
```

Editing or rescheduling is allowed only before a terminal state. Cancellation changes the job to
`cancelled`; it does not delete its audit history. `send-now` still applies attachment-integrity and
final-confirmation checks. If another worker has already claimed the job, the command reports a
conflict rather than racing it.

## Undo Send is not recall

When `undo_send_seconds` is greater than zero, pressing Send creates an `undo_delay` job. During the
countdown the message remains local:

```text
gucmail scheduled undo SCHEDULE_REFERENCE
```

After the countdown, the scheduler can transmit it. Undo Send cannot retrieve a message already
accepted by Exchange. Exchange recall, if a server exposes it, is a separate unreliable operation
and is not represented as Undo Send. The configured default delay is `0` seconds, so Undo Send is
off until enabled.

## Reliability and duplicate prevention

The scheduler stores jobs and history in SQLite and claims work transactionally. Each job has an
idempotency key, attachment snapshot, content hash, attempt count, next retry, and lease. A worker
recovers stale claims after an abrupt exit. Multiple workers do not intentionally send the same
claimed job.

Known pre-request network failures can retry with bounded exponential backoff and jitter.
Authentication failures move to `waiting_authentication`. Connectivity failures move to
`waiting_connectivity` or `retry_scheduled` as appropriate. Exchange back-off instructions take
precedence.

A timeout after the request may have crossed the network is different: the state becomes
`send_state_uncertain`. It is never retried blindly. Review Sent Items and the local correlation
information; if the outcome cannot be reconciled safely, the job becomes
`manual_review_required`.

Attachments are revalidated before sending. A missing or changed local file blocks the send until
the draft/job is reviewed. A job that requires final confirmation is not sent unattended.

## Sleep, missed times, offline use, and password changes

- **Computer asleep:** nothing is transmitted. An OS scheduler configured with “start when
  available” invokes the one-shot runner after wake.
- **TUI closed:** sending still works if the OS scheduler or another supervisor runs
  `scheduler run-once`.
- **No internet:** the job waits and retries conservatively; it does not disappear.
- **Password changed:** the job waits for authentication. Update the keyring once; do not repeatedly
  retry the old password.
- **Missed time:** the next scheduler run evaluates the overdue job. The scheduled time is not
  rewritten, so history shows that it was late.
- **Time-zone change:** the stored instant remains stable. Reschedule explicitly if the intended
  wall-clock time should move with the user.
- **Daylight-saving transition:** nonexistent or ambiguous local times require resolution before
  scheduling. An explicit UTC offset avoids ambiguity.

## Recurring messages

Recurring automation is local and disabled until explicitly created. Each occurrence gets its own
auditable message instance and job; one mutable Exchange draft is not repeatedly reused. Supported
plans can be daily, weekly, monthly, selected weekdays, count-limited, or end-date-limited.

```text
gucmail recurring list
gucmail recurring create
gucmail recurring pause RECURRING_REFERENCE
gucmail recurring skip RECURRING_REFERENCE
gucmail recurring resume RECURRING_REFERENCE
```

Preview recipients, template content, attachments, recurrence, and whether each occurrence needs
manual approval before activation.

## Foreground and one-shot operation

```text
gucmail scheduler status
gucmail scheduler run
gucmail scheduler run-once
gucmail scheduler pause
gucmail scheduler resume
gucmail scheduler doctor
```

`run` is a foreground worker. `run-once` checks due work and exits, which is the preferred OS
scheduler integration. The generated default interval is 300 seconds and cannot be below 60
seconds. Exact send time can therefore be later than the requested instant by the runner interval,
startup, network, or retry delay.

## Windows Task Scheduler

First preview the generated plan:

```text
gucmail scheduler install
```

The plan creates a least-privilege task named **GUC Mail Scheduler**. Its command is equivalent to:

```text
PYTHON_EXECUTABLE -m gucmail --quiet scheduler run-once
```

The generated task ignores overlapping runs, starts when available after a missed trigger, permits
battery use, requires network, and limits one run to 15 minutes. Installation and removal are
mutating local-system operations and require explicit confirmation. Inspect `gucmail scheduler
install --help` for the confirmation/apply flags exposed by the installed version.

After installation:

```powershell
schtasks.exe /Query /TN "GUC Mail Scheduler" /V /FO LIST
```

Test with an empty queue or a synthetic local job; documentation verification must not send live
mail. To remove the integration:

```text
gucmail scheduler uninstall
```

Uninstalling the OS task does not delete drafts, jobs, or history. If the task uses an interactive
credential store, the Windows user must be logged in when it runs.

## Troubleshooting

Use `gucmail scheduled list`, `gucmail scheduler status`, and `gucmail scheduler doctor`. Check the
current state, attempts, last error, next retry, attachment status, and whether confirmation is
required. For uncertain sends, do not use `send-now`; reconcile first. See
[Troubleshooting](../troubleshooting/index.md).

## Security and privacy

The local queue contains private recipients, message content, attachment paths, and audit history.
Protect the user data directory. Schedules cannot invoke hooks or send without the configured
authorization. Scheduler logs redact addresses and never contain message bodies or credential
values.

## Preview, confirmation, and local recovery

`send --dry-run` validates recipients, text, and files locally, including with `--schedule` or
`--undo-delay`. It does not authenticate, create a draft, or add a job. Queueing a send requires
confirmation (or explicit `--yes` for automation). `--undo-delay 0` overrides the saved delay for
that invocation. Explicit scheduling takes precedence over the saved Undo Send preference.

The TUI runs a one-shot countdown for its own Undo Send job while open and online. Cancel it in
Scheduled Mail. Closing the TUI leaves the job durable; a separate scheduler must run it. Retries
also require that scheduler. The countdown respects cancellation, rescheduling, and pause state.

Before sending, the worker rechecks draft content and attachments. Incomplete drafts require manual
review without contacting Exchange. The explicit CLI empty-subject/body overrides survive queue
persistence. Editing a scheduled snapshot requires reviewing those exceptions again.

The composer preserves recovery text before validation or authentication can fail. If a referenced
attachment disappears, the recovery copy keeps the text and attachment path and requires file
review. It cannot silently send with the missing file.

Generated OS scheduled-send jobs use `gucmail.scheduler_entry` with explicit configuration,
data, cache, and log paths. They retain the queue selected when the plan was generated.
