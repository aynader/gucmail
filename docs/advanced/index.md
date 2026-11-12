# Advanced workflows

Advanced mode is a presentation choice; it does not expand Exchange permissions or change mailbox
semantics.

For every core TOML field, path override, type, default, and environment variable, use the
[configuration reference](../configuration/index.md).

## Search and analysis

`gucmail search` performs a bounded server search. `gucmail index build` creates a disposable local
FTS5 index; `index update`, `verify`, `optimize`, `rebuild`, and `clear` manage it. Fuzzy ranking and
clustering are local and bounded. Optional semantic search uses the local `local-lsa-v1` model
with TF-IDF and truncated SVD. Check `gucmail semantic status`, explicitly enable it, and review
`semantic build` before applying a model build to an existing local index. It needs an available
local NumPy runtime; it does not download a model or send mailbox text to a remote service.
Use `semantic update` after index changes, and `semantic clear` or `disable` to remove the model
or stop using it. Ordinary indexed text search remains available without semantic search.

## Automation

Use `gucmail --json` or `--ndjson`, stable exit codes, and `--non-interactive`. Scheduled sends live
in the durable local state database. `gucmail scheduler run-once` is the supervisor-safe entry
point. See [Scheduling](../scheduling/index.md) and [Monitoring](../monitoring/index.md).

## EWS diagnostics

Use `gucmail ews operations`, `describe`, `support-matrix`, and `diagnostics`. Registry status is
static until a safe live probe occurs. `not_probed` does not mean supported or unsupported.
Privileged and destructive operation families are registry-only unless a typed, safety-classified
adapter exists.

There is no arbitrary raw-SOAP execution command in this release. `ews request-preview` classifies
an operation without constructing XML or sending a request. This is intentional: an unvalidated
XML escape hatch would bypass typed serialization, request bounds, and mutation confirmation.
The legacy raw-execution preference is shown as unavailable and cannot be enabled in Settings;
Expert mode does not create an XML executor. The internal request loader rejects DTD declarations
after parsing document metadata as well as checking bytes, including encoded UTF-16 input.

## Local data boundaries

Cache is disposable, the FTS index is rebuildable, scheduler/draft state is durable application
state, and a backup is an independently verifiable archive. They are not interchangeable. See
[Local data and retention](../privacy-and-security/local-data-locations-and-retention.md).

## Mailbox delegates

`gucmail ews delegates` reads the configured mailbox's delegate identities, six reported folder permission levels, meeting-copy preference and private-item access. In the TUI, use the Advanced palette command **Show Mailbox Delegates**. Refresh performs one read; Previous and Next navigate the loaded collection locally. No permissions are changed, and delegate creation, modification and deletion remain unavailable.

EWS GetDelegate does not provide server pagination: `--limit` (default 100) and `--offset` slice one response locally, with a 1000-delegate safety cap. Missing permission fields are shown as **Not reported**, distinct from the explicit **None** permission level. This view does not enumerate every shared folder, effective access permission, Send As grant or delegated mailbox. Server version eligibility and this account's access remain unprobed; a permission error is not a claim that GUC never supports delegation.
