# Choose notification destinations and routing

The watcher creates one normalized new-mail event, applies local rules and privacy policy, then routes the allowed result to one or more providers. Providers are isolated: a failure in Discord, a generic webhook, or a desktop notification must not prevent mailbox synchronization or delivery to another provider.

## Available destination types

**Windows desktop** shows a local toast while the signed-in desktop session can receive it. Choose generic, sender-only, sender-and-subject, or sanitized-preview privacy. Lock-screen visibility deserves particular care.

**Discord** uses a secret incoming-webhook URL and a Discord-compatible payload. Mentions are escaped by default, embeds obey Discord field limits, and rate limits use bounded retry rather than notification floods.

**Generic webhook** sends an allowlisted HTTPS payload with a stable event ID. Advanced options include custom method and headers, HMAC signing, idempotency, timeouts, retry limits, and destination safety checks. Redirects may not downgrade from HTTPS.

**Slack-compatible and Microsoft Teams-compatible webhooks** use provider-specific payload templates when installed. Do not assume a generic JSON body will display correctly. Test with synthetic content in a channel you control.

**Telegram** needs a bot token and target chat identifier stored through secret references. It remains disabled unless its provider is configured and enabled. Use restrictive chat membership and a private payload.

**JSON or NDJSON output** writes structured local events for pipes, files, or a supervisor. This is useful on a server and does not contact an external destination by itself. Protect the output if it contains sender or subject fields.

**Local command hooks** are expert-only and disabled by default. Arguments are an explicit vector, never a shell-expanded string. Event text cannot become executable syntax. Prefer a webhook or NDJSON consumer when possible.

## Route deliberately

A monitoring profile chooses watched folders, strategy preference, poll interval, rules, quiet hours, digests, provider policies, and routing. Rules can match sender, subject, importance, categories, attachment state, folder, working hours, and other normalized fields. Test rules against a synthetic event before enabling delivery.

Quiet hours can suppress, defer, or collect a digest. Provider-specific filtering happens after the global rule result so private fields do not leak to a destination that was not selected. Duplicate suppression uses the stable event ID; delivery history records redacted outcomes and retry state.

Start with [Notification rules](help:notifications.rules). Then use [Discord setup](help:integrations.discord) or [Generic webhooks](help:integrations.webhooks). A synthetic provider test is available through [Test notifications](command:notifications.test); it never reads or changes mailbox content.

## Safe setup sequence

1. Create or select a monitoring profile.
2. Choose the least revealing privacy mode that meets the need.
3. Add secret material through the secure provider workflow.
4. Create a narrow routing rule and inspect its explanation.
5. Preview a synthetic event locally.
6. Send one explicitly confirmed provider test.
7. Review redacted notification history.
8. Enable the watcher only after health checks pass.

Never paste a provider URL, token, HMAC key, or copied mail into Help, command-line arguments, screenshots, or diagnostics.
