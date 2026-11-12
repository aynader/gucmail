"""Shared terminal presentation for the form-led GUC Mail frontend."""

FRONTEND_CSS = """
.frontend Button.-style-default {
    height: 1; min-height: 1; min-width: 0; border: none; padding: 0 1;
    background: transparent; color: $gucmail-primary-text; text-style: none;
}
.frontend Button.-style-default:hover, .frontend Button.-style-default:focus {
    border: none; background: $gucmail-hover; color: $gucmail-primary-accent; text-style: none;
}
.frontend Button.-style-default.-active {
    border: none; background: $gucmail-active; tint: #000000 0%; text-style: none;
}
.frontend Button.-style-default.-primary {
    border: none; background: $gucmail-primary-accent; color: auto 100%;
}
.frontend Button.-style-default.-warning { color: $gucmail-error; }
.frontend Button.-style-default:disabled {
    border: none; background: transparent; color: $gucmail-disabled-text;
    opacity: 1; text-opacity: 100%;
}
.frontend Input {
    border: none; border-bottom: solid $gucmail-divider; padding: 0 1;
    background: $gucmail-app-background; color: $gucmail-primary-text;
}
.frontend Input:focus { border: none; border-bottom: solid $gucmail-focus-ring; }
.frontend SelectCurrent {
    border: none; border-bottom: solid $gucmail-divider;
    background: $gucmail-app-background; color: $gucmail-primary-text;
}
.frontend Select:focus SelectCurrent { border-bottom: solid $gucmail-focus-ring; }
.frontend SelectOverlay {
    background: $gucmail-elevated-surface; border: solid $gucmail-border;
}
/* Settings owns a one-cell search field above a separate divider. A generic
   input border would consume the field's only content row. */
SettingsScreen.frontend #settings-search,
SettingsScreen.frontend #settings-search:focus {
    border: none; height: 1; min-height: 1; padding: 0 1;
    background: transparent; color: $gucmail-primary-text;
}
SettingsScreen.frontend #settings-footer .area-close { padding: 0; }
SettingsScreen.frontend #settings-tabs Button {
    height: 2; padding: 0; color: $gucmail-secondary-text;
}
SettingsScreen.frontend #settings-tabs Button.active {
    border-bottom: solid $gucmail-primary-accent; color: $gucmail-primary-text;
}
.frontend Checkbox { border: none; background: transparent; padding: 0 1; }
.frontend TextArea {
    border: solid $gucmail-divider; background: $gucmail-app-background;
}
.frontend TextArea:focus { border: solid $gucmail-focus-ring; }
.frontend DataTable { background: $gucmail-app-background; scrollbar-size: 1 1; }
.frontend DataTable > .datatable--header {
    background: $gucmail-app-background; color: $gucmail-secondary-text; text-style: none;
}
.frontend DataTable > .datatable--odd-row,
.frontend DataTable > .datatable--even-row { background: $gucmail-app-background; }
.frontend DataTable > .datatable--cursor {
    background: $gucmail-selection-background; color: $gucmail-selection-foreground;
}
.frontend Footer {
    height: 2; min-height: 2; border-top: solid $gucmail-divider; padding: 0 1;
    background: $gucmail-app-background;
}
.frontend .workspace-heading {
    background: transparent; color: $gucmail-primary-text; padding: 0; height: 1;
}
.frontend .workspace-actions {
    height: auto; min-height: 1; layout: horizontal; padding: 0; margin-top: 1;
}
.frontend .workspace-actions.many-actions {
    layout: grid; grid-size: 4; grid-rows: 1;
}
.frontend.workspace-narrow .workspace-actions { layout: grid; grid-size: 3; grid-rows: 1; }
.frontend .workspace-actions Button { width: 1fr; margin: 0; }
.frontend .workspace-title { color: $gucmail-primary-text; }

#workspace-tabs {
    height: 3; padding: 0 2; border-bottom: solid $gucmail-divider;
    background: $gucmail-app-background;
}
#workspace-tabs Button { width: auto; height: 2; padding: 0; margin-right: 3;
    color: $gucmail-muted-text; }
#workspace-tabs Button.active { border-bottom: solid $gucmail-primary-accent;
    color: $gucmail-primary-text; }
.narrow #workspace-tabs { height: 2; padding: 0 1; }
.narrow #workspace-tabs Button { margin-right: 2; }
#folder-tree { padding: 1 1 0 1; background: $gucmail-sidebar-background; }
#navigation { background: $gucmail-sidebar-background; }
#folder-tree > .tree--guides { color: $gucmail-divider; }
#folder-tree > .tree--cursor { color: $gucmail-primary-accent;
    background: $gucmail-selection-background; text-style: none; }
#folder-tree > .tree--highlight { background: $gucmail-hover; }
#message-heading { height: 2; padding: 1 2 0 2; }
#message-filter { height: 2; margin: 0 1; background: transparent;
    border: none; border-bottom: solid $gucmail-divider; }
#message-filter:focus { background: transparent; border-bottom: solid $gucmail-focus-ring; }
#message-table { padding: 0 1; background: $gucmail-message-list-background; }
#message-table > .datatable--header { background: $gucmail-message-list-background; }
#preview-scroll { padding: 2 3; }
#contacts-search { width: 100%; height: 2; margin: 0 0 1 0; }
#status-line, #status-bar { background: $gucmail-footer; color: $gucmail-muted-text; }
CompactHeader { background: $gucmail-header; }
StorageScreen #storage-overview { height: auto; margin: 1 0; color: $gucmail-primary-text; }
StorageScreen #storage-evidence {
    height: auto; padding: 0; border: none; margin-bottom: 1; background: transparent;
}
StorageScreen #storage-summary { height: auto; min-height: 0; max-height: 30; }
StorageScreen #storage-body { height: 1fr; min-height: 7; }
StorageScreen #storage-analysis-pane { height: 1fr; min-height: 7; }
StorageScreen #storage-analysis-table { min-height: 4; }
StorageScreen #storage-detail { height: 4; padding: 1 0; color: $gucmail-muted-text; }
StorageScreen.compact-storage #storage-summary { min-height: 0; max-height: 30; }
StorageScreen.compact-storage #storage-body { height: 1fr; min-height: 7; }
StorageScreen.compact-storage #storage-analysis-pane { height: 1fr; }
StorageScreen.compact-storage #storage-detail { height: 3; }
ActivityScreen #activity-overview { height: auto; margin: 1 0; }
ActivityScreen #activity-monitoring-details {
    height: auto; padding: 0; border: none; background: transparent;
}
ActivityScreen #activity-watcher { height: auto; min-height: 0; max-height: 20; }
ActivityScreen.compact-activity #activity-watcher { min-height: 0; max-height: 20; }

ReaderScreen #reader-screen-scroll {
    height: 1fr; padding: 2 3; align-horizontal: center;
    background: $gucmail-app-background;
}
ReaderScreen #reader-screen-content { width: 88; max-width: 100%; }
FloatingReader { border: solid $gucmail-border; background: $gucmail-app-background; }
FloatingReader #floating-reader-titlebar { height: 2; background: $gucmail-app-background; }
FloatingReader #floating-reader-title { padding: 0 1; }
FloatingReader #floating-reader-close { height: 1; border: none; }

PaletteScreen { background: $gucmail-app-background 85%; }
PaletteScreen #palette-box { border: solid $gucmail-border;
    background: $gucmail-command-palette-background; padding: 1; max-height: 25; }
PaletteScreen #palette-input { height: 2; margin: 0 1; }
PaletteScreen #palette-explanation {
    height: auto; min-height: 2; max-height: 6; margin: 1; text-wrap: wrap;
}
PaletteScreen .palette-category { background: transparent; color: $gucmail-muted-text; }
PaletteScreen .palette-item { height: auto; min-height: 2; padding: 0 1; }
PaletteScreen #palette-hint { background: transparent; color: $gucmail-muted-text; }

ComposeScreen { background: $gucmail-app-background 95%; }
ComposeScreen #composer {
    width: 92%; max-width: 104; height: 94%; padding: 1 2;
    border: none; background: $gucmail-app-background;
}
ComposeScreen #compose-heading { height: 2; border-bottom: solid $gucmail-divider; }
ComposeScreen #compose-brand { width: 13; height: 1; color: $gucmail-logo-primary; }
ComposeScreen .dialog-title { width: 1fr; height: 1; margin: 0; }
ComposeScreen #compose-options-toggle { width: 16; color: $gucmail-muted-text; }
ComposeScreen #compose-fields { height: 1fr; padding-top: 1; overflow-x: hidden; }
ComposeScreen .compose-row { height: 2; }
ComposeScreen .compose-label { height: 1; padding: 0; color: $gucmail-muted-text; }
ComposeScreen .compose-row Input { height: 2; min-height: 2; }
ComposeScreen #compose-extra-addresses { margin-left: 8; height: 1; color: $gucmail-muted-text; }
ComposeScreen #compose-attachment-controls { height: 1; margin: 1 0; }
ComposeScreen #compose-attachment-controls.has-files { height: 3; }
ComposeScreen #compose-attachment-controls Button { height: 1; }
ComposeScreen #compose-attachment-controls #compose-attachment-add { width: 17; }
ComposeScreen #compose-options-area { height: auto; }
ComposeScreen #compose-delivery-options { height: 3; }
ComposeScreen #compose-delivery-options #compose-importance { width: 25; margin-right: 2; }
ComposeScreen #compose-sent-copy, ComposeScreen #compose-options Checkbox {
    height: 1; margin-top: 1;
}
ComposeScreen .compose-body-label { height: 1; color: $gucmail-muted-text; margin-bottom: 1; }
ComposeScreen #compose-body { border: none; background: $gucmail-app-background; min-height: 8; }
ComposeScreen #compose-body > .text-area--cursor-line { background: $gucmail-app-background; }
ComposeScreen #compose-note { height: auto; max-height: 5; color: $gucmail-muted-text; }
ComposeScreen #compose-actions { height: 2; min-height: 2; border-top: solid $gucmail-divider; }
ComposeScreen #compose-actions Button { height: 1; min-height: 1; }
ComposeScreen.compact-compose #composer { width: 100%; height: 100%; padding: 0 1; }
ComposeScreen.compact-compose #compose-actions Button { min-width: 5; }
ComposeScreen.compact-compose #compose-actions #compose-save { width: 15; }
ComposeScreen.compact-compose #compose-actions #compose-send { width: 16; }

ConfirmationScreen #confirm-box, ConfirmationScreen #operational-confirm-dialog,
ReaderModeScreen #reader-mode-box, PaletteParameterReviewScreen #parameter-review-box,
PaletteSequencePreviewScreen #sequence-box, AreaScreen #area-dialog {
    border: solid $gucmail-border; background: $gucmail-modal-background;
}
"""
