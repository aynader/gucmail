# View, download, and inspect attachments safely

Attachment metadata is listed without automatically downloading content. A save operation retrieves only the selected attachment and keeps the destination beneath the chosen directory.

## Download protections

Filenames are normalized, traversal components are removed, and collisions create a new name unless overwrite was explicitly confirmed. Declared and observed sizes are checked against the configured limit. Risky executable or script extensions receive a warning and are never opened automatically. Optional SHA-256 output lets you verify the saved bytes.

Item attachments can contain another Exchange item and may not behave like ordinary files. Progress and cancellation matter for large content; partial output should be written atomically or removed.

## Content extraction

Indexing attachment text is disabled by default. Safe extractors use strict input, output, and time limits and reject active XML, macros, unsafe external relationships, encrypted documents, and suspicious archives. They never execute content or perform OCR. A failure status such as unsupported, unsafe, too large, encrypted, or timeout is preferable to unsafe fallback behavior. See [Local search](help:search.local).
