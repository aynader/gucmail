"""Preserve fail-closed bootstrap for explicitly owned synthetic test children."""

import os
from collections.abc import Mapping


def guarded_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {**os.environ, **(overrides or {})}
    # A test may deliberately minimize its ordinary environment; it may not
    # replace the parent's guard roots/bootstrap with an unprotected process.
    environment.update(
        (key, value)
        for key, value in os.environ.items()
        if key.startswith("GUCMAIL_AUDIT_") or key == "PYTHONPATH"
    )
    return environment
