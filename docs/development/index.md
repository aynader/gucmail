# Development guide

Use Python 3.11 through 3.14. The pinned Exchange dependency is `exchangelib==5.6.0` because the
adapter and typed-extension contracts depend on that API surface.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,attachments]"
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\pytest.exe -q
.\.venv\Scripts\python.exe -m build
$env:GUCMAIL_REQUIRE_RELEASE_ARTIFACTS = "1"
.\.venv\Scripts\pytest.exe tests\test_package_artifacts.py -q
```

Tests are offline and synthetic by default. Live tests require the explicit `live` marker and a
separate opt-in. Never read, print, or execute a credential file in a fixture. Do not send, move,
delete, flag, categorize, or mark real messages read during automated verification.

New mailbox operations must add a domain plan/protocol, adapter mapping, capability classification,
bounds, safety class, confirmation behavior, fixture tests, help text, and support documentation.
Never let an `exchangelib` object escape the adapter.

The capability registry and command help are importable offline. Documentation tests validate
internal links and required command names. The required artifact gate checks wheel/sdist contents,
installs the candidate wheel from an empty working directory, and runs offline import, resource,
version, and help smoke checks. To keep that gate offline, its temporary virtual environment reuses
already-installed runtime dependencies through a plain path entry, without loading the source
checkout's editable-install hooks. The application wheel is installed with `--no-index --no-deps`.
That test verifies artifact isolation, not clean dependency resolution. Separately, install the
wheel and declared dependencies into a fully empty environment from an approved wheelhouse or
package index. Build before the artifact tests or a full suite whenever source has changed: the
gate deliberately rejects stale packaged source.


The logo-sample provenance check uses the immutable `tests/fixtures/guc-logo-source.png`
input. Its hash must agree with `GUC_LOGO_SOURCE_SHA256`; do not regenerate or replace the
fixture to make a failing assertion pass. Source-distribution tests verify that this
specific fixture is included byte for byte. Root screenshots and reference images remain
excluded from release archives. This allows the provenance test to run from an extracted
source distribution without relying on a developer checkout's root image.
