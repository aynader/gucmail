# Internal API guide

The supported user API is the `gucmail` executable. Python modules are internal and may evolve, but
the main extension boundaries are intentionally narrow:

- mailbox gateway protocols in service modules;
- `EWSGateway` for provider translation;
- `BackupSource` and `RestoreTarget` for streamed backup/restore;
- notification providers and webhook delivery abstractions;
- domain dataclasses in `gucmail.models`;
- the versioned `gucmail.output/v1` JSON envelope.

Application services accept domain values and return immutable plans/results. A mutation should be
split into `plan_*` and execution when users need to preview scope. Update plans must contain an
explicit nonempty field list; unrestricted saves of partially fetched EWS objects are forbidden.

The raw state database and backup object layout are not a general-purpose public API. Use service
methods so migrations, locking, verification, redaction, and confirmation invariants remain intact.

