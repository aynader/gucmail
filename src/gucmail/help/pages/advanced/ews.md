# Exchange, EWS, SOAP, and capability status

EWS capability has independent axes: a typed implementation path, detected server-version eligibility, authenticated account access, verification evidence, and safety class. “Implemented” never proves that this server permits the operation.

## Inspect status

Use the operation registry, describe, support-matrix, and diagnostics commands. Values such as not probed, unsupported server version, disabled by configuration, unauthorized, administrative only, fixture tested, and live read-only verified have different meanings.

The main adapter uses the pinned Exchange library. Narrow SOAP extensions need fixed namespaces, serializer order, strict XML parsing, typed responses, explicit field bounds, and fault handling. An HTTP 200 response can still contain an EWS operation error.

Arbitrary raw SOAP execution is unavailable in this release. Expert mode and the unavailable Raw SOAP setting cannot authorize or transmit arbitrary XML. Use registered typed operations for supported actions; `gucmail ews request-preview FindItem` classifies a named operation without transmitting XML. Mutations require typed serialization, confirmation, and ambiguous-result handling. Read-only retries honor server backoff; send, update, move, and delete are not blindly retried after an uncertain transport failure.

Exact quota, directory, rules, recoverable items, notifications, archive, and calendar features can vary independently.

## Mailbox delegates

`gucmail ews delegates` reads the configured mailbox's delegate identities, six reported folder permission levels, meeting-copy preference and private-item access. In the TUI, use the Advanced palette command **Show Mailbox Delegates**. Refresh performs one read; Previous and Next navigate the loaded collection locally. No permissions are changed, and delegate creation, modification and deletion remain unavailable.

EWS GetDelegate does not provide server pagination: `--limit` (default 100) and `--offset` slice one response locally, with a 1000-delegate safety cap. Missing permission fields are shown as **Not reported**, distinct from the explicit **None** permission level. This view does not enumerate every shared folder, effective access permission, Send As grant or delegated mailbox. Server version eligibility and this account's access remain unprobed; a permission error is not a claim that GUC never supports delegation.
