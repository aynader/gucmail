# Find and change settings safely

Settings are non-secret preferences grouped by appearance, layout, mail, composer, calendar, contacts, notifications, integrations, storage, backup, search, connection, privacy, and advanced behavior.

## Use the Settings form

The top tabs are **General**, **Mail**, **Appearance**, **Accounts**, and **Advanced**.
The first four show common preferences in a numbered form. Select a row with Tab,
the arrow keys, or a click. Enter opens its editor. **Keep change** stages the
value; **Save** or Ctrl+S applies staged changes. **Discard** or Ctrl+Z cancels
them. Escape closes an editor before returning from Settings.

Use Ctrl+F to search the entire catalog. **All mail settings** (or the equivalent
link in another tab) opens the full category. **Advanced** provides all categories
and visibility levels. **More** contains import/export, history, resets, and the
theme and branding browsers. Existing command palette routes still open their
exact category or preference.

## Inspect before changing

Use Settings search or `gucmail config show` for the current redacted view. Fuzzy and natural-language lookup should return the canonical setting, current safe value, default, description, valid range, restart requirement, and related Help. Basic, Advanced, and Expert modes change presentation, not server permission or safety semantics.

`gucmail config set KEY VALUE` validates a single supported field before writing. Replace KEY with the setting ID and VALUE with its desired value; quote values containing spaces. Invalid endpoints, aggressive polling, unknown zones, missing themes, and unsafe values must fail without replacing the last good file. Credentials and webhook secrets never belong in normal configuration.

Export must redact secrets and private free text. Import first produces a diff. Reset is scoped by category or all preferences and should preserve operational state, drafts, schedules, and backups. Configuration revisions and corruption recovery belong in Activity or diagnostics. See [Credentials](help:privacy.credentials) and [Themes](help:appearance.themes).
