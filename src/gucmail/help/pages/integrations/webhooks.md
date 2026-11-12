# Generic outbound webhooks

A webhook lets GUC Mail send a small HTTP notification to another application when an event occurs, such as receiving a new email. The local watcher makes an outbound request after it detects and filters the event. It does not expose an inbound public server and does not make Exchange contact this computer.

## Create a destination safely

A definition has a name, HTTPS destination reference, permitted method, public or keyring-backed headers, timeout, retry limit, field allowlist, redaction policy, and optional HMAC signing-key reference. Authorization, cookies, API keys, destination URLs containing secrets, and signing keys belong in the operating-system keyring.

The CLI accepts a destination from standard input or an existing `keyring://` reference so it does not appear in process listings or shell history. New definitions remain disabled until tested:

```text
gucmail webhooks add example-hook --url-stdin --field event_id --field subject
gucmail webhooks test example-hook
gucmail webhooks enable example-hook
gucmail webhooks list
```

The first test is a network-free synthetic preview. Add `--deliver` only for one explicitly confirmed request to a destination you control. Removing a definition needs the normal confirmation and does not alter mailbox content.

## Payload, privacy, and verification

Every event has a stable ID suitable for an idempotency key. The allowlist controls which normalized fields can leave the computer. Start with `event_id`, generic account identity, folder, local message reference, timestamp, importance, and attachment presence. Add sender or subject only when the receiver needs them. Bodies, attachment content, credentials, cookies, authorization values, and raw EWS identifiers stay excluded unless an explicit supported policy says otherwise.

HMAC signing lets an advanced receiver verify the exact request body. Store the shared key in both systems securely. The receiver should compute the signature over the unmodified bytes, compare it in constant time, check an acceptable timestamp if included, and treat a repeated idempotency key as the same event.

## Destination and retry safety

Only supported HTTPS URLs are accepted. User information in URLs, malformed ports, unsafe redirects, downgrade to HTTP, and accidental loops are blocked. Private-network targets require the application's explicit destination policy because they can reach services unavailable to the public internet.

Retries apply only to classified temporary failures. They use bounded exponential backoff with jitter, honor valid provider retry hints, and stop at the configured maximum. Each attempt records a sanitized outcome. A permanent 4xx error normally requires configuration repair; repeated delivery should not become a notification flood.

## Troubleshooting

Check the redacted destination name, HTTP status class, attempt count, next retry, event ID, timeout stage, TLS result, and HMAC status. Never log or share the URL, authorization header, signing key, or payload fields containing private mail. Use [Notification troubleshooting](help:troubleshooting.notifications) and compare [provider choices](help:integrations.providers).
