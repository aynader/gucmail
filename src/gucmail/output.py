"""Stable human, JSON, and NDJSON output contract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rich.console import Console

from gucmail import __version__
from gucmail.formatting import compact_rows, json_text, page_parts, render_table, setting_rows
from gucmail.models import to_primitive
from gucmail.security import sanitize_terminal

OUTPUT_SCHEMA = "gucmail.output/v1"


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"
    NDJSON = "ndjson"


@dataclass(slots=True)
class OutputWriter:
    format: OutputFormat = OutputFormat.HUMAN
    quiet: bool = False
    no_color: bool = False
    console: Console = field(init=False)
    error_console: Console = field(init=False)

    def __post_init__(self) -> None:
        self.console = Console(no_color=self.no_color, stderr=False)
        self.error_console = Console(no_color=self.no_color, stderr=True)

    def envelope(
        self,
        data: Any,
        *,
        command: str,
        warnings: list[str] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        result = {
            "schema": OUTPUT_SCHEMA,
            "application_version": __version__,
            "command": command,
            "data": to_primitive(data),
            "warnings": warnings or [],
        }
        if source:
            result["source"] = source
        return result

    def emit(
        self,
        data: Any,
        *,
        command: str,
        title: str | None = None,
        warnings: list[str] | None = None,
        source: str | None = None,
    ) -> None:
        if self.quiet:
            return
        envelope = self.envelope(data, command=command, warnings=warnings, source=source)
        if self.format is OutputFormat.JSON:
            self.console.print_json(json_text(envelope))
            return
        if self.format is OutputFormat.NDJSON:
            page = page_parts(envelope["data"])
            page_meta: dict[str, Any] = {}
            if page is not None:
                # A bounded page streams one record per item (the documented
                # contract); paging metadata moves to the summary record.
                values, page_meta = page
            else:
                values = list(data) if isinstance(data, (list, tuple)) else [data]
            for value in values:
                record = {
                    "schema": OUTPUT_SCHEMA,
                    "type": "item",
                    "command": command,
                    "data": to_primitive(value),
                }
                self.console.print(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    markup=False,
                    highlight=False,
                    soft_wrap=True,
                )
            summary = {
                "schema": OUTPUT_SCHEMA,
                "type": "summary",
                "command": command,
                "count": len(values),
                "warnings": warnings or [],
            }
            if page_meta:
                summary["page"] = page_meta
            self.console.print(
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            return
        primitive = to_primitive(data)
        page = page_parts(primitive)
        notes: list[str] = []
        if page is not None:
            primitive, meta = page
            notes.extend(self._page_notes(meta, shown=len(primitive)))
        if isinstance(primitive, list) and all(isinstance(item, dict) for item in primitive):
            rows, hidden = compact_rows(primitive) if primitive else ([], 0)
            render_table(rows, title=title, console=self.console)
            if hidden:
                notes.append(f"{hidden} more field(s) per item are available with --json.")
        elif isinstance(primitive, dict):
            render_table(setting_rows(primitive), title=title, console=self.console)
        else:
            self.console.print(
                sanitize_terminal(primitive) if isinstance(primitive, str) else primitive,
                markup=False,
                highlight=False,
            )
        for note in notes:
            self.console.print(sanitize_terminal(note), markup=False, highlight=False)
        for warning in warnings or []:
            self.error_console.print(
                f"Warning: {sanitize_terminal(warning)}",
                style="yellow",
                markup=False,
                highlight=False,
            )

    @staticmethod
    def _page_notes(meta: dict[str, Any], *, shown: int) -> list[str]:
        notes: list[str] = []
        plan = meta.get("plan")
        if isinstance(plan, dict):
            reasons = plan.get("fallback_reasons")
            if isinstance(reasons, list):
                notes.extend(f"Note: {reason}" for reason in reasons if isinstance(reason, str))
        if meta.get("has_more") and meta.get("next_offset") is not None:
            notes.append(
                f"Showing {shown} item(s). More may be available: rerun with "
                f"--offset {meta['next_offset']}."
            )
        return notes

    def warn(self, message: str) -> None:
        if not self.quiet:
            self.error_console.print(f"Warning: {message}", style="yellow")
