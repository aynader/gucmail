# Command-line and structured-output reference

The installed `--help` output is authoritative for exact arguments and options. Put global options before the command. Running without a subcommand must not surprise scripts by silently starting an interactive workflow.

## Discovery and output

Use `gucmail --help`, `gucmail GROUP --help`, and `gucmail GROUP COMMAND --help`. Human-readable output is the default. JSON uses a versioned envelope; NDJSON emits bounded item records followed by a summary. Quiet, no-color, verbose, debug, non-interactive, and confirmation flags have stable global meaning.

Non-interactive mode fails closed when confirmation is required. `--yes` acknowledges a reviewed scope; it does not bypass validation, capability checks, permanent-delete friction, or uncertain-send safeguards. Lists have default and maximum limits.

The searchable command reference should be generated from the canonical registry so descriptions, aliases, parameters, safety, availability, shortcuts, and Help links cannot drift. Exit codes distinguish usage, configuration, authentication, network, not found, conflict, unsupported, partial success, and local storage errors. See [Command palette](help:palette.overview).
