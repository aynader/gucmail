# Themes, contrast, and color

A theme is a complete palette, not a renamed alias. It should define background and surface layers, normal and muted text, accents, selection, focus, borders, statuses, mail states, calendar/contact/task colors, syntax, and branding treatment.

## Choose and preview

Use the installed theme browser to search categories, preview sample mail and dialogs, and check whether a theme is dark, light, high contrast, monochrome-friendly, or reduced-color. A preview does not save until applied. Favorites and recent themes are local preferences.

Custom themes should start from a complete base, validate every required token, and report contrast problems before import. Export contains palette and display settings but no credentials or mailbox content. A theme name is valid only when the current catalog lists it; Help does not promise a specific catalog count.

`--no-color` and monochrome mode must remain readable without relying on red versus green alone. Small terminals and limited color capabilities need graceful fallback. See [GUC ASCII branding](help:appearance.branding) and [Layout](help:appearance.layout).
