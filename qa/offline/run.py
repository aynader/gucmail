"""Guarded entry point. Invoke all product audit checks through this script."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, default=REPO / ".tmp-offline-audit-20261029")
    parser.add_argument("--output", type=Path, default=REPO / "docs/audit/2026-10-29")
    parser.add_argument("--fixture-credentials", action="store_true")
    parser.add_argument("mode", choices=("preflight", "module", "pytest", "script"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    os.environ["GUCMAIL_AUDIT_PRIVATE"] = str(args.private.resolve())
    os.environ["GUCMAIL_AUDIT_OUTPUT"] = str(args.output.resolve())
    os.environ["GUCMAIL_AUDIT_FIXTURES"] = "1" if args.fixture_credentials else "0"
    os.environ["GUCMAIL_AUDIT_RUN_ID"] = uuid4().hex
    from qa.offline.guard import install_from_env

    guard = install_from_env()
    status = 0
    try:
        if args.mode == "pytest":
            import pytest

            class Trace:
                def pytest_runtest_logstart(self, nodeid, location):
                    os.environ["GUCMAIL_AUDIT_ACTIVE_TEST"] = nodeid

                def pytest_runtest_logreport(self, report):
                    with (guard.private / "test-progress.jsonl").open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "test": report.nodeid,
                                    "phase": report.when,
                                    "outcome": report.outcome,
                                    "seconds": report.duration,
                                }
                            )
                            + "\n"
                        )
                    if report.failed:
                        with (guard.private / "test-failures.txt").open(
                            "a", encoding="utf-8"
                        ) as log:
                            log.write(report.nodeid + "\n" + report.longreprtext + "\n")

            status = int(
                pytest.main(
                    [
                        "-m",
                        "not live",
                        "-o",
                        f"cache_dir={args.private / 'pytest-cache'}",
                        "-o",
                        f"log_file={args.private / 'pytest.log'}",
                        f"--basetemp={args.private / 'pytest-work'}",
                        *args.arguments,
                    ],
                    plugins=[Trace()],
                )
            )
        elif args.mode == "script":
            if Path(args.arguments[0]).name.startswith("capture_"):
                from textual.app import App
                from textual.widgets import Button, DataTable

                original_capture = App.save_screenshot
                contexts = []

                def observed_capture(app, *capture_args, **capture_kwargs):
                    result = original_capture(app, *capture_args, **capture_kwargs)
                    selected = getattr(app, "active_theme", None)
                    contexts.append(
                        {
                            "file": Path(result).name,
                            "screen": type(app.screen).__name__,
                            "mount_host": type(app).__name__,
                            "theme": getattr(selected, "id", getattr(app, "theme", "unknown")),
                            "size": [app.size.width, app.size.height],
                            "operation_active": getattr(app.screen, "_operation_active", False),
                            "render_pending": getattr(app.screen, "_render_pending", False),
                            "tables": [
                                {"id": table.id, "rows": table.row_count}
                                for table in app.screen.query(DataTable)
                            ],
                            "out_of_terminal_buttons": [
                                {
                                    "id": button.id,
                                    "disabled": button.disabled,
                                    "region": list(button.region),
                                }
                                for button in app.screen.query(Button)
                                if button.display
                                and button.region.width
                                and button.region.height
                                and not app.screen.region.contains_region(button.region)
                            ],
                        }
                    )
                    (Path(result).parent / "capture-contexts.json").write_text(
                        json.dumps(contexts, indent=2) + "\n"
                    )
                    return result

                App.save_screenshot = observed_capture
            sys.argv = list(args.arguments)
            runpy.run_path(args.arguments[0], run_name="__main__")
        else:
            module = "qa.offline.preflight" if args.mode == "preflight" else args.arguments[0]
            sys.argv = [module, *([] if args.mode == "preflight" else args.arguments[1:])]
            runpy.run_module(module, run_name="__main__")
    except SystemExit as exc:
        status = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
    finally:
        events = guard.run_events()
        unexpected = [event for event in events if not event["expected"]]
        summary = {
            "unexpected_blocks": len(unexpected),
            "expected_blocks": len(events) - len(unexpected),
        }
        (args.private / f"run-{os.getpid()}.json").write_text(json.dumps(summary), encoding="utf-8")
        print("OFFLINE_GUARD " + json.dumps(summary))
    return status or (90 if unexpected else 0)


if __name__ == "__main__":
    raise SystemExit(main())
