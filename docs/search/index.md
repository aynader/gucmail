# Search

Press `/` in the app to filter the list you're looking at. For anything bigger, use
**Ctrl+K → Search mail**, or the `gucmail search` command.

Every result says where it came from. **live_server** is the GUC server, **search_index** is
the optional index on your computer, and **backup** is a backup you made.

## Search the server

```text
gucmail search "project update"
gucmail search --sender teacher@example.invalid --after 2026-01-01 --has-attachments
gucmail search --all-folders --subject "exam schedule"
```

You can also type filters straight into the search box.

```text
from:teacher@example.invalid
to:student@example.invalid
subject:"exact phrase"
after:2026-01-01 before:2026-09-01
has:attachment attachment:pdf
unread:true flagged:true importance:high
folder:inbox category:"Courses"
```

Without `--folder` or `--all-folders`, search looks in your default folder. `--all-folders`
covers up to 50 mail folders and 200 results. If your mailbox is bigger, gucmail asks you to
narrow the search instead of giving partial results.

Searching never marks mail as read.

## Saved searches

Save searches you run often. Only the search is saved, not the results.

```text
gucmail saved-searches add unread-august "unread:true after:2026-08-01"
gucmail saved-searches run unread-august
gucmail saved-searches list
gucmail saved-searches remove unread-august
```

In the app, **Ctrl+K → Saved searches** lets you run, edit, rename and delete them.

## Typos

```text
gucmail search "projetc update" --fuzzy
gucmail contacts search "Mona Adle" --fuzzy
```

Exact matches come first. Each close match says why it matched. `--fuzzy-threshold` sets how
close a match must be, from 0 to 100 (default 82).

## Search on your computer

The local index makes search fast and works offline. It's off until you build it, and it's a
copy, not a backup.

```text
gucmail index build
gucmail index update
gucmail index status
gucmail index clear
```

`index build` reads one page of recent mail headers. Add `--include-bodies` to also index
message text. Nothing is marked as read.

### Text inside attachments

Off by default. Turn it on in **Settings → Search & Indexing → Attachment text indexing** and
choose the file types. TXT, CSV, JSON, XML and HTML work out of the box. PDF, Word, PowerPoint
and Excel need the `attachments` extra.

```text
pipx install --force "gucmail[desktop,attachments]"
```

Files are read with strict size and time limits. Macros, scripts and links never run.

### Search by meaning

Optional, and off by default. It learns which words go together in your own mail and can find a
message even when it uses different words. It runs only on your computer and never sends mail to
an AI service. It needs the `semantic` extra.

```text
gucmail semantic status
gucmail --yes semantic enable
gucmail --yes semantic build --apply
gucmail semantic search "deadline change"
gucmail --yes semantic clear
```

Without `--apply`, `build` only shows what it would do.

## Group similar mail

Clusters group mail by topic, sender, conversation, time or attachment type. They help you find
what takes space or what you can tidy up.

```text
gucmail clusters build --by topic
gucmail clusters list
gucmail clusters show CLUSTER_REFERENCE
```

Building clusters never deletes or sends anything.

## Mail you haven't answered

```text
gucmail insights unanswered --self-address me@example.test
```

Lists conversations where someone else wrote last. Add every address you send from. Only headers
are read.

## Text in images

On Windows, gucmail can read text from a saved image using Windows' own text recognition.
Nothing is uploaded.

```text
gucmail ocr status
gucmail --yes ocr extract saved-image.png --language en-US --apply
```

Recognised text can contain mistakes.

## Privacy

The index and semantic data contain text from your mail. They stay on your computer.
`gucmail index clear` and `gucmail semantic clear` delete them. Your mail on the server is not
touched.
