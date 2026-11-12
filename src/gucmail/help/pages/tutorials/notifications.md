# Tutorial: test notifications with synthetic data

Use a generated event so provider formatting can be checked without accessing a mailbox. The example should contain only a fictional sender and subject.

## Practice steps

1. Review configured notification providers and their privacy fields.
2. Run `gucmail notifications test` without any delivery option. Inspect the synthetic preview.
3. If a local desktop provider is installed, choose its explicit local delivery test and confirm the operating-system notification.
4. Open notification history and verify that the provider result contains no password, address from a real account, body, attachment content, or raw server identifier.
5. Keep external webhook delivery disabled during this tutorial.

A successful synthetic test validates formatting and provider invocation, not continuous mailbox detection. Start monitoring separately, accept a quiet baseline, and inspect its checkpoint. If desktop delivery is unavailable, continue using console or local event-stream output and read [Windows notification availability](help:notifications.windows).
