# Notifications or background monitoring stopped

Use the user-visible symptom to choose a check. Do not repeatedly launch watchers or authenticate several times; single-instance protection and Exchange backoff exist to protect the account.

## No notifications appear

1. Run `gucmail headless status` and check whether the watcher is running, paused, or waiting for credentials.
2. Run `gucmail headless health` and inspect checkpoint age, last successful cycle, active strategy, next retry, and configuration problems.
3. Open notification history. A suppressed rule, quiet hours, digest schedule, duplicate event, privacy policy, or provider failure should have an explicit outcome.
4. Test locally with `gucmail notifications test`. A synthetic test does not read mailbox content. External provider delivery still needs explicit selection and confirmation.
5. If the password changed, update the one secure keyring entry and restart the supervised worker once.

Polling delay can be up to the configured interval. Do not call polling instant. Sleep pauses a local watcher; after wake it should reconnect, honor backoff, perform a bounded catch-up, and keep duplicate suppression. Read [Monitoring](help:monitoring.overview) for strategy details.

## The watcher is already running

This normally means single-instance protection found an active owner. Inspect status instead of starting another copy. If status proves the owner is stale, use the supervised stop or repair workflow; do not delete lock files blindly. Two workers can duplicate provider deliveries and compete for state.

## Windows startup stopped working

Run `gucmail startup status`. If the scheduled task exists but its executable, arguments, working directory, or credential context changed, preview `gucmail startup repair` before confirming. Repair must preserve conservative interval and single-instance settings. See [Start monitoring with Windows](help:startup.windows).

## Desktop notification failed

Confirm that the Windows provider is installed, notifications are allowed for the application, Focus Assist or Do Not Disturb is not suppressing it, and a signed-in desktop session exists. A server without an interactive desktop should use Discord, a webhook, or NDJSON instead.

## Discord or webhook delivery failed

An HTTP 401 or 403 usually means the stored secret is invalid or no longer authorized. Replace it through secure provider settings; never print it for comparison. HTTP 404 commonly means the webhook was removed. HTTP 429 requires honoring the provider's retry delay. Timeouts use bounded retry and the stable event ID. An HTTPS validation or private-address safety failure should be fixed at the destination rather than bypassed.

Use one synthetic test after repair. Review [Discord setup](help:integrations.discord), [Generic webhooks](help:integrations.webhooks), and [provider routing](help:integrations.providers). If mailbox synchronization succeeds while one provider fails, leave the watcher running and repair only that isolated provider.

## Information safe to collect

Record the sanitized error class, provider kind, event ID, redacted delivery state, retry count, last checkpoint time, watcher strategy, and correlation ID. Exclude webhook URLs, tokens, headers, message bodies, attachment data, and personal addresses. Doctor can generate a reviewable diagnostic bundle.
