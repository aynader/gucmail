# Development guide

## Set up

Use Python 3.11 through 3.14.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,attachments]"
```

## Check your change

```powershell
.\.venv\Scripts\ruff.exe check src tests qa
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\python.exe qa\test.py
```

`qa\test.py` runs the whole suite in parallel inside the offline guard: no network, no
credentials, no real mailbox. It takes a few minutes and deletes everything it wrote when it
finishes. Pass pytest arguments to run less, e.g. `qa\test.py tests\test_cli.py -k reply`.

Live tests are skipped unless you explicitly opt in. Automated checks never send, move,
delete, flag or mark real messages read.

## Where things live

- `src/gucmail/cli/`: the command line, one module per area (`mail.py`, `calendar.py`, ...).
  Shared plumbing (output, confirmation, the mailbox connection) is in `cli/core.py`; tests
  patch helpers there, e.g. `monkeypatch.setattr(cli.core, "gateway", ...)`.
- `src/gucmail/tui/app.py`: the main window. Its behaviour is split into mixins by theme
  (`app_mail.py`, `app_compose.py`, `app_palette.py`, ...). State set up at start is in
  `app_core.py:AppCore`, which also declares, for type checking only, the methods the parts
  call on each other.
- `src/gucmail/ews/`: everything that talks to Exchange. No `exchangelib` object may leave it.
- `tests/fixtures/server/`: shapes recorded from the real GUC server. Refresh with
  `qa/record_server_shapes.py` (read-only, and only when you mean to touch the live account).

New mailbox operations need a plan, an adapter mapping, a capability entry, limits,
confirmation for anything destructive, tests and help text.

## Release

1. Set the version in `src/gucmail/__init__.py`. It is the only place it is written.
2. Add a `## X.Y.Z` section at the top of `CHANGELOG.md`, written for users.
3. Merge to `main` with CI green, then tag and push:

   ```powershell
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The Release workflow checks that the tag, version and changelog agree, runs CI, builds,
publishes to PyPI through trusted publishing (no token) and creates the GitHub release with
the changelog section as its notes.

One-time setup on PyPI: add a trusted publisher for project `gucmail` with owner `aynader`,
repository `gucmail`, workflow `release.yml` and environment `pypi`. On GitHub, create an
environment named `pypi`.

## Packaging checks

`tests/test_package_artifacts.py` checks the wheel and sdist contents and installs the wheel
into an empty environment. Build first (`python -m build`) and set
`GUCMAIL_REQUIRE_RELEASE_ARTIFACTS=1` to make missing artifacts fail instead of skip.

The logo provenance check uses the fixed `tests/fixtures/guc-logo-source.png`; its hash must
match `GUC_LOGO_SOURCE_SHA256`. Do not replace the fixture to make a failing test pass.
