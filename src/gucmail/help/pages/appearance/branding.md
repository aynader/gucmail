# GUC ASCII branding

Terminal branding can use an image-derived GUC logo, stylized GUC or GUC Mail text, compact and minimal forms, no banner, or validated custom text. The selected preset should adapt to terminal size and current theme.

## Safe display

Preview width and height before saving. Wide artwork must not expand the application header or push the message list below the available screen. Narrow terminals should switch to a compact form, and monochrome terminals need a shape-based fallback rather than color alone.

The familiar compact wordmark uses black G, red U, and yellow C when the palette and terminal can represent them. A theme may adjust surrounding contrast while preserving the three-letter identity. Custom text is sanitized before terminal rendering and cannot contain control sequences.

Image-derived presets should come from the packaged, reviewed logo resource and deterministic conversion settings. Font-backed variants must be real renderings, not hundreds of labels pointing to the same output. The browser should load metadata lazily because large catalogs do not belong in startup-critical code.

## Font catalog and fallbacks

The branding browser includes the complete offline font set distributed by `pyfiglet`, with dedicated Featured FIGlet, ANSI FIGlet, and TOIlet-compatible filters. A font is rendered only when you preview or apply it, so catalog breadth does not generate hundreds of banners during startup. Styles named by the Secondpass design but absent from the installed font backend remain searchable and are visibly labelled `(fallback)`; their preview states that Standard is being used. This is an explicit compatibility fallback, not a second label for identical hidden output.
