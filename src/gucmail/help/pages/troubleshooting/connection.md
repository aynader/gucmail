# Connection, login, timeout, and stale-session problems

Start with the visible state and one controlled diagnostic pass. Rapid retries can lock an account or worsen Exchange throttling.

## Diagnose in order

1. Run local status and Doctor without network access.
2. Confirm account identity, username, HTTPS endpoint, system time, and keyring availability.
3. Opt into DNS/TLS checks once.
4. Opt into one bounded authenticated status check only when credentials are intentionally being tested.
5. Read the translated error class: authentication, network, timeout, throttling, unsupported, unauthorized, or local state.

A long-lived session can become stale after sleep, proxy changes, or server closure. Blocking calls need timeouts and cancellation; the UI should end its spinner and offer a bounded reconnect. Authentication retries remain stricter than read-only network retries. Server Retry-After or backoff data takes precedence.

## A change is still running

Cancel, Escape, a timeout, or closing a workspace stops waiting for its result.
An Exchange request that already started may still complete. While that request
is running, the open application rejects another change from its other screens,
including mail, folders, drafts, Calendar, Contacts, Tasks and delayed sends.
This warning does not undo the first request. A request cancelled before its
worker thread starts is skipped.

Wait for the original request to finish, then refresh its target and inspect the
result before choosing another change. Reads on another screen remain available.
Rejected changes are not queued or retried. If a reviewed composer submission is
blocked, its local recovery copy remains available through Drafts. A blocked Undo
Send timer leaves its job queued; review it in Scheduled Mail or use the configured
scheduler. The guard belongs to this open app; durable send claims and local-store
conflict checks still handle other processes and uncertain send outcomes.

If a password changed, update the keyring once instead of launching several watchers. An unsupported or unauthorized feature was not performed. See [Doctor](help:diagnostics.doctor) and [EWS capability status](help:advanced.ews).
