# Integrations

Outbound webhooks are disabled by default, HTTPS-only, bounded, redacted, and protected against
private-network targets unless explicitly allowed by policy. Definitions store a keyring reference,
not a plaintext secret.

```text
gucmail webhooks add example --field event_type
gucmail webhooks list
gucmail webhooks test example
gucmail --yes webhooks test example --deliver
gucmail --yes webhooks enable example
gucmail webhooks disable example
gucmail webhooks remove example
```

The interactive `add` command reads the destination through a hidden prompt. Automation must pipe
one URL line from a secret manager to `--url-stdin`, or supply an existing namespaced
`keyring://` locator with `--destination-reference`. Raw destination URLs are intentionally not
accepted as command arguments because process listings and shell history can expose them.

Delivery uses a stable event ID/idempotency key, HMAC signing when a secret is configured, bounded
timeouts, retry history, and redirect blocking. `webhooks test` is a network-free synthetic preview
by default. `--deliver` performs exactly one confirmed outbound test and reports the HTTP delivery
result. The test uses synthetic data and does not access the mailbox.

Use `--method` for POST, PUT, or PATCH, repeated `--header NAME=VALUE` options for public headers,
and `--max-retries` for a bounded retry limit. Authorization, cookie, and API-key headers must use a
`keyring://entry` value; plaintext secret header values are rejected. A signing secret is referenced
with `--secret-keyring-name` and is resolved only at delivery time. `watch` dispatches events to
enabled definitions; these are outbound local requests, not Exchange server-side webhooks.

OS scheduler integration is generated locally and applied only with `--apply` plus confirmation.
No cloud integration, analytics SDK, or telemetry transport is enabled implicitly.

For the built-in chat payload adapter, follow [Discord notification setup](discord.md).

For optional local notifications, follow [Windows desktop setup](windows-desktop.md).
