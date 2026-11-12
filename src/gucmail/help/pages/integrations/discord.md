# Discord notification privacy and setup

Discord notifications forward selected new-mail alerts to a channel through an incoming webhook. A webhook URL is a secret: anyone who obtains it may be able to post in that channel. Store it through the secure provider workflow, never ordinary configuration, screenshots, shell history, Help search, or diagnostics.

## Beginner setup

1. Open Discord and choose a server and channel you control.
2. Open the channel's integration settings.
3. Create an incoming webhook.
4. Copy its URL, but do not paste it into a chat or command argument.
5. In GUC Mail open **Settings → Notifications & Integrations → Discord**.
6. Choose **Add Discord Webhook** and paste the URL into the protected secret field.
7. Choose a privacy mode: generic, sender only, sender and subject, or a bounded sanitized preview.
8. Choose which notification rules may route to Discord. Start with a narrow synthetic rule.
9. Preview a synthetic event, including how `@everyone`, `@here`, user mentions, and long fields are escaped or truncated.
10. Send one explicitly confirmed synthetic test and save only after it appears correctly.

The provider hides the URL after storage. Replace it by saving a new protected value; remove it through provider settings or the keyring-backed removal workflow. A generic JSON webhook is not automatically Discord-compatible, so use the Discord provider or a trusted relay that performs the conversion.

## Command-line setup

Use `gucmail webhooks add --help` to review the installed secret-input options. A generic HTTPS destination can be read from standard input with `--url-stdin` or referenced through a `keyring://` name. It is created disabled by default. Preview testing performs no network delivery:

```text
gucmail webhooks list
gucmail webhooks test example-discord
gucmail webhooks enable example-discord
```

Use `webhooks test NAME --deliver` only for one confirmed synthetic external request to a destination you control. Do not place the URL itself in the command line.

## Privacy and rate limits

Bodies, attachment content, raw Exchange identifiers, credentials, and unrestricted headers are excluded by default. A routing rule can narrow sender, subject, importance, category, folder, or attachment state before Discord is selected. Discord field limits, mention escaping, stable event IDs, duplicate suppression, and bounded retry prevent malformed messages and floods.

HTTP 401 or 403 normally means the stored secret is invalid or unauthorized. HTTP 404 often means the Discord webhook was deleted. HTTP 429 requires the provider's retry delay. Test once after repair. Delivery history must show only redacted destination metadata and the sanitized outcome.

Continue with [Notification rules](help:notifications.rules), [provider routing](help:integrations.providers), and [notification troubleshooting](help:troubleshooting.notifications).
