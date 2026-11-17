# Using Help

Help is a complete offline manual, not just a shortcut card. It searches article titles, headings, body text, aliases, canonical commands, settings, natural-language examples, error signatures, and glossary terms. No question or mailbox detail is sent to an online search engine or language model.

## Start at the right place

Open Help with `?`, choose a category in the table of contents, or focus search with `Ctrl+F`. Search accepts ordinary questions such as `why did mail stop loading`, exact command names such as `backup.verify`, setting names such as `monitoring.poll_interval_seconds`, and sanitized error text. Misspellings such as `webhok` and `calender` are tolerated.

The home view keeps the manual approachable. It shows Start Here topics, suggestions for the current screen, popular tasks, symptom-led troubleshooting, local favorites, and recent pages. Help opened from Calendar, Composer, Storage, Backup, Settings, or an error should prioritize that context. Use `Alt+Left` and `Alt+Right` for history, `Ctrl+Home` for Help home, and `Ctrl+D` to favorite the current page.

Every article has a breadcrumb and heading outline. Related-topic links remain inside bundled Help. External HTTPS links never open automatically. This protects offline use and prevents a documentation page from unexpectedly contacting another service.

## Commands, settings, and examples

Typed links can connect an article to a command preview or the relevant Settings entry. A safe read-only test may be run only after its preview is visible. Sending, deletion, external delivery, credential access, and other changes require their normal review and confirmation; opening Help never bypasses those controls.

## Guided practice

Choose **Tutorials** in the Help toolbar, or press `Alt+T`, then select a lesson. When viewing a lesson's guide article, its related lesson opens first; other articles start with navigation. The command palette also offers **Guided Tutorials**. Choose among navigation, preparing a first message, the palette, natural-language search, scheduling, Windows notifications and backups.

Each lesson has a short exercise and review decision. **Check** validates your answer; **Next** advances after a successful check and **Finish** records completion. Use **Back** to revisit a step, **Restart** to clear only the selected lesson's progress, or Escape/**Close** to skip and return. **Guide** opens the full article; closing that article returns to your lesson.

The exercises use synthetic metadata and local parsers. They cannot send mail, create a scheduled job, deliver a notification, read a mailbox or make a backup. Real workflows remain separate and use their usual reviews. Only lesson ID, position and completion are stored in account-isolated local UI state; practice text is discarded. Lessons remain usable with session-only progress when storage is unavailable. Completion means that the exercises passed, not that a provider or unattended delivery was verified.

Use these offline CLI surfaces when the TUI is unavailable:

```text
gucmail help toc
gucmail help search "mail not loading"
gucmail help show troubleshooting.connection
gucmail help error "Connection timed out"
gucmail help commands
gucmail help shortcuts
gucmail help glossary
gucmail help validate
```

The command reference is generated from the canonical registry, the settings reference from the settings schema, and the natural-language library from the tested parser corpus. This keeps names, defaults, safety labels, connectivity requirements, and examples aligned with the installed application.

## Privacy and accessibility

Help follows the active theme, monochrome or high-contrast choice, reduced-motion preference, text wrapping, and narrow-terminal layout. Search history, recent pages, and favorites are small non-secret local records. Do not paste passwords, webhook URLs, message bodies, private addresses, or unreviewed diagnostic output into an error search.

If an article appears inconsistent with the application, run [Validate Help](command:help.validate). It checks links, commands, settings, shortcuts, parser fixtures, synthetic examples, and packaged resources without contacting Exchange.

CLI examples are checked for command names, option placement, aliases, required arguments and values. Put global options first, as in `gucmail --json help validate`; a command's own options follow that command. Replace uppercase placeholders such as BACKUP_ID with your actual reference. Validation never runs example commands or checks their files, credentials, account permissions or delivery results.
