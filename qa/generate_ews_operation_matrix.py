"""Regenerate the checked-in EWS capability matrix from the canonical registry."""

from __future__ import annotations

import argparse
from pathlib import Path

from gucmail.ews.capabilities import DEFAULT_REGISTRY
from gucmail.models import Capability

DEFAULT_OUTPUT = Path("docs/ews/operation-matrix.md")


def _cell(value: object) -> str:
    """Render one Markdown table cell without allowing row injection."""

    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()


def _row(capability: Capability) -> str:
    detail = capability.fallback or capability.reason or "None documented"
    minimum = capability.minimum_version or "None"
    return (
        f"| `{_cell(capability.operation)}` | {_cell(capability.family.replace('_', ' '))} | "
        f"`{capability.implementation_path.value}` | "
        f"`{capability.implementation_status.value}` | `{capability.safety.value}` | "
        f"`{capability.server_support.value}` | `{capability.account_access.value}` | "
        f"`{capability.verification.value}` | `{_cell(minimum)}` | {_cell(detail)} |"
    )


def render_operation_matrix() -> str:
    lines = [
        "# EWS operation coverage matrix",
        "",
        "This file is generated from the canonical static capability registry for this release.",
        "Dynamic server and account fields remain `not_probed` until a deliberately bounded live",
        "capability probe records evidence; `gucmail ews support-matrix` is the executable view.",
        "",
        "| Operation | Family | Path | Implementation | Safety | Server | Account | "
        "Verification | Minimum version | Fallback / reason |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *(_row(capability) for capability in DEFAULT_REGISTRY.all()),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the output file differs instead of rewriting it.",
    )
    arguments = parser.parse_args()
    rendered = render_operation_matrix()
    if arguments.check:
        current = (
            arguments.output.read_text(encoding="utf-8") if arguments.output.is_file() else None
        )
        if current != rendered:
            raise SystemExit(f"EWS operation matrix is stale: {arguments.output}")
        return
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
