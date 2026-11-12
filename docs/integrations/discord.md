# Discord notification setup

Discord delivery is an outbound notification provider. Exchange never calls Discord directly: the
local watcher detects a new message, applies the active profile's privacy/rule policy, renders a
Discord-safe payload, and sends it to the configured incoming webhook.

## Create the Discord webhook

In the target Discord server, open the channel settings, choose **Integrations**, create an incoming
webhook, choose the destination channel, and copy the webhook URL. Treat that URL as a password: it
contains a token that can post to the channel.

## Configure GUC Mail CLI

Open the TUI Settings area, choose **Notifications**, add a **Discord** provider, and paste the URL
into the masked secret field. The URL is validated as HTTPS on an approved Discord webhook host and
stored through a secret reference rather than the ordinary TOML configuration. Choose the profile
that should use the provider and keep it disabled until the preview is correct.

Create or select a watcher profile separately:

```text
gucmail notifications profiles create primary --name "Primary mailbox" --folder Inbox --activate
gucmail notifications profiles enable primary
gucmail notifications providers list
```

Provider listing and profile display redact destinations and never print the webhook token.

## Test safely

A provider test is synthetic and network-free by default:

```text
gucmail notifications providers test --provider discord
```

After reviewing the rendered fields in Settings, explicitly choose the deliver option in the TUI
or run the command's help-listed `--deliver` flow. That sends one synthetic notification; it does not
authenticate to Exchange or include a real sender, subject, or message body.

Start one bounded watcher pass, then inspect delivery history:

```text
gucmail watch --profile primary
gucmail notifications history
```

The default quiet baseline prevents a new profile from flooding the channel with old mail.

## Privacy and troubleshooting

- Prefer sender domain, importance, timestamp, and a short sanitized subject. Do not enable body
  previews or stable EWS IDs unless the channel's membership and retention policy permit them.
- Discord rejects oversized content; the adapter truncates and sanitizes user-controlled fields.
- A `401` or `404` normally means the webhook was revoked or copied incorrectly. Replace the secret
  reference; do not paste the URL into logs or issue reports.
- A `429` is rate limiting. The dispatcher records the attempt and uses bounded backoff; do not run
  rapid manual tests.
- If a URL is exposed, delete/regenerate it in Discord and remove the old provider secret.

See [Integrations](index.md) for provider security and [Headless deployment](../monitoring/headless-deployment.md)
for running the watcher under a supervisor.

