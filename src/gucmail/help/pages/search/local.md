# Local index, fuzzy search, attachments, and clusters

The local full-text index is optional derived data. It can be rebuilt or removed and is never a substitute for a verified backup. Body caching, attachment indexing, and semantic search remain off until explicitly enabled.

## Index and fuzzy behavior

Build and update operate in bounded batches without marking messages read. Verification checks database integrity, schema version, and row counts. Fuzzy search normalizes punctuation and tolerates spelling errors while preserving source text; exact matches rank above approximate ones and results explain their score.

Safe ordinary-file extraction supports a limited format set, size ceiling, output ceiling, and timeout. It does not run macros, scripts, external relationships or links. Image OCR requires the separate consent described below. Unsafe, encrypted, oversized, or unsupported files receive an explicit status.

Clusters are deterministic local groupings by sender, conversation, time, topic terms, recipient group, or attachment type. A cluster can seed another previewed action but does not delete, notify, or back up anything by itself. Semantic search uses the opt-in `local-lsa-v1` corpus-topic model; absence of the optional fitting dependency keeps lexical search and existing model queries usable.

## Local Search workspace

The TUI workspace searches the disposable SQLite FTS index, completed unencrypted backups, or both. Backup search includes archived mail, contacts, calendar records, and tasks when those kinds exist in the archive. Result rows show source, record type, score, title, folder, date, and a safe snippet. The detail pane distinguishes an FTS match, a bounded fuzzy match, stale backup content, lexical similarity, and duplicate evidence.

Similar-message analysis compares no more than 5,000 indexed records. Exact duplicate groups require a complete-content or raw-MIME SHA-256 digest. Matching indexed text without that proof is labelled **possible**, and near-duplicate comparison is bounded. Search never performs cleanup; use Storage/Cleanup to preview a separately confirmed action.

## Optional local topic search

The **Semantic search** palette command opens local topic ranking. **Semantic setup** offers consent, selected record limit/dimensions, optional already indexed body chunks, preview, confirmed build/update/retrain, disable and confirmed clear. Use Tab and Shift+Tab for the scrollable controls; Ctrl+C cancels an operation. Changed fields invalidate a preview.

The optional `semantic` extra supplies NumPy only for fitting. No dependency or pretrained model is downloaded by the application. `local-lsa-v1` learns word co-occurrence from the selected local corpus; it has no pretrained language knowledge or guaranteed multilingual understanding. Results label `local-semantic`, explain normalized topic similarity and lexical overlap, and remain bounded to the saved selection. Queries of a built model do not require NumPy.

Subjects are selected by default (500 latest local records, maximum 32 topics). Bodies require an explicit opt-in and use only existing indexed text. Limits are 2-2000 records, subject 8192 characters, body 32768 per record, first 2048 training chunks, at most 2048 terms/96 dimensions, model JSON 8 MiB and semantic SQLite 64 MiB. Fitting has an owned process and 60-second deadline; dense input is at most 32 MiB plus SVD working memory. The form previews these costs before confirmation.

The account-bound semantic file saves vocabulary/basis, title/folder metadata, lexical term overlap data and embeddings. It does not save raw body chunks, but derived data still reveals content. No remote AI service is called and no Exchange connection is needed. Update transforms changed records and removes records no longer selected; rebuild retrains the topic basis. Body-policy/dimension changes require rebuild. Changed local text invalidates search until update; cancellation, conflicts and failed transactions retain preceding data.

CLI: `semantic status`, `semantic enable` (confirmed), `semantic build` (preview), `semantic build --apply` (confirmed), `semantic update --apply` (confirmed), `semantic search QUERY`, `semantic disable`, `semantic clear` (confirmed). `--include-body` and `--rebuild` deliberately change the build. Global `--yes` supplies reviewed non-interactive confirmation.

Disable retains the model until clear. Clear removes the model/vocabulary/embeddings and compacts SQLite while retaining the lexical index, mail and backups. Filesystem snapshots and physical forensic erasure are outside this guarantee. FTS, fuzzy, backup, similarity and duplicate search remain available when semantic processing is disabled or fitting is unavailable.


## Optional local image OCR

The **Open Local Image OCR** palette command opens a local file/language form. Choose
an already saved PNG/JPEG and an installed Windows language, Preview, explicitly enable
this image, and confirm Recognize. Default is disabled; changing choices clears consent
and results. Tab/Shift+Tab reach the scrollable form and result. Ctrl+C cancels an owned
worker; Escape returns. Ordinary text extraction and local search remain available
without a usable OCR engine or language. No installation or download is attempted.

CLI: `ocr status` inspects installed languages; `ocr extract IMAGE --language TAG`
previews. `--apply` plus confirmation (`--yes` for reviewed automation) recognizes.
8 MiB input, 2048 pixels per dimension, first image only, default 50,000/max 100,000
output characters and default 8/max 15 seconds bound recognition. The temporary image
copy is removed afterward; no OS memory quota or forensic erasure is claimed. Network,
mapped-drive and symbolic paths are rejected. Output is sanitized and labelled
`ocr_attachment_text`, with truncation exposed; review it for recognition errors.
No confidence score, mailbox download, cloud recognition or automatic indexing is
provided. Automatic OCR attachment indexing/global persistence remains outside this workflow.


## Reliable local index rebuild

`index rebuild` confirms replacement of the entire disposable index by one bounded mailbox
page. The CLI can explicitly fetch bodies with `--include-bodies`; Diagnostics never fetches
bodies. Both consume the reviewed current-machine attachment policy. New rows are prepared before one atomic commit. Read/validation/SQL failure,
cancellation observed before commit, or a changed index revision keeps the previous index.
Maximum 500 documents and 32 MiB of prepared UTF-8 field/payload data; this is not a total
process-memory limit. An empty successful replacement clears the old rows. Build/update still
commits individual rows and retains earlier progress if a later read fails. Repeat the selected
page to refresh it; no dedicated build checkpoint is claimed.

The **Rebuild index** palette command requires **Expert** Settings complexity under Advanced.
It opens the folder/limit review before any mailbox read. Decline leaves the existing index
alone; a competing write invalidates that review even when row count and clock time match.

## Attachment index settings and review

Settings → Search & Indexing → **Attachment text indexing** is disabled by default. Stage,
Apply, Cancel and Reset use the normal current-machine preferences; a saved change starts no
background download. CLI build/update/rebuild and Expert Diagnostics rebuild consume this policy.
Enabled indexing requires a separate confirmation before downloading the selected attachments;
changed settings invalidate the preview. Decline leaves the previous index alone.

The default file types are TXT/CSV/JSON/XML/HTML (`.htm` also works). Optional PDF/DOCX/PPTX/XLSX
require explicit selection and already installed parser dependencies. Default limits are 5 MiB
per file, 8 seconds per parser, 5 files per message, 25 downloads/20 MiB bytes per run, and
50,000 extracted characters per message. Settings expose finite ceilings. Stream probes share
the byte budget. Exchange request timeout is separate; no Windows memory quota is claimed.

PNG/JPEG also require **Image OCR in attachment indexes** (off by default) and an installed
**Attachment index OCR language**. No engine/language download occurs. Owned temporary image
copies are removed. OCR text is labelled and may be inaccurate; unavailable recognition or a
failed file receives an outcome while ordinary text extraction remains usable. Ctrl+C stops
an owned parser/recognizer; pre-commit rebuild cancellation keeps the old index.

The local index retains private extracted text, sanitized filenames, labelled outcomes and
truncation. Search reads those fields without another download. Disable stops future extraction;
confirmed `index clear` removes existing derived content, without forensic-erasure guarantees.
Calendar, contact and task archive search is separate. The per-image OCR consent remains a
separate workflow and does not enable these saved attachment producer preferences.
