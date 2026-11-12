# Exchange automatic replies

Automatic replies are stored on Exchange and can respond to incoming mail. They are separate from
the local notification rule engine, quiet hours and provider routing.

In the TUI, open Notifications and choose **Automatic replies** from Overview, or use **Manage
Automatic Replies** in the palette. The initial read does not enable replies. Edit the state,
external audience and messages; scheduled start/end times need an explicit UTC offset. Review
the policy, then confirm it. Escape cancels either step without writing.

```text
gucmail automatic-replies show
gucmail --json automatic-replies set --state enabled --external-audience none --internal-reply-file internal.txt --dry-run
gucmail --yes automatic-replies set --state enabled --external-audience none --internal-reply-file internal.txt
gucmail --yes automatic-replies set --state disabled
```

Files must contain UTF-8 text, up to 128 KiB each. External audiences are `none`, `known` and `all`.
An external audience requires its own external message; internal content is never copied into
an empty external reply. Unspecified fields retain the current policy. For `scheduled`, supply
`--start` and `--end` using ISO timestamps, and ensure the end follows the start and is in the future.

The service compares the current policy with the preview before the write and reads it back
afterwards. It does not replay an accepted or uncertain write. A changed preview requires refresh;
failed or mismatched readback produces a partial result and instructions to inspect current state.
No local reply cache or schema migration is needed.

Whether your account may change automatic replies depends on the server and your account
permissions. A server denial is reported as a capability/access failure and nothing is changed.
