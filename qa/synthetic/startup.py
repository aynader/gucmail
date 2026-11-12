"""Fresh guarded process startup with explicit shell, populated and cleanup stages.

No OS disk-cache flushing is attempted. Fresh-process and guard-bootstrap time
are reported separately; they are not called cold-storage measurements.
"""

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from qa.offline.guard import install_from_env


def startup_context(root):
    # Match the ordinary context's real configuration/state services without
    # importing screenshot drivers and their unrelated workflow screens.
    from gucmail.bootstrap import AppContext
    from gucmail.config import AppConfig, AppPaths, ConfigStore
    from gucmail.credentials import CredentialResolver, CredentialStore
    from gucmail.state import StateStore

    paths = AppPaths(
        config_dir=root / "config",
        data_dir=root / "data",
        cache_dir=root / "cache",
        log_dir=root / "logs",
        config_file=root / "config/config.toml",
        state_database=root / "data/state.sqlite",
        recovery_dir=root / "data/recovery",
        index_database=root / "data/index.sqlite",
        backup_dir=root / "data/backups",
    )
    store = ConfigStore(paths)
    config = AppConfig(email="synthetic@example.test")
    store.save(config)
    return AppContext(
        paths=paths,
        config_store=store,
        config=config,
        state=StateStore(paths.state_database, account_key="synthetic-demo"),
        credential_resolver=CredentialResolver(CredentialStore()),
    )


def child(scale):
    guard = install_from_env()
    launched = int(os.environ["GUCMAIL_AUDIT_LAUNCH_NS"])
    stages = {}

    def stamp(name):
        stages[name] = (time.perf_counter_ns() - launched) / 1_000_000
        print(f"STARTUP_STAGE {name} {stages[name]:.3f}", file=sys.stderr, flush=True)

    stamp("guard_ready_ms")
    if scale == "guard":
        print(json.dumps({"stages": stages}))
        return
    import asyncio

    from textual.widgets import DataTable

    from gucmail.tui.app import GUCmailApp
    from qa.synthetic.benchmark import memory
    from qa.synthetic.mailbox import LocalMailbox

    stamp("presentation_imported_ms")

    async def run():
        context = startup_context(guard.private / ("startup-profile-" + uuid4().hex))
        gateway = LocalMailbox(guard.private / "datasets" / scale / "mailbox.sqlite")
        context._gateway = gateway
        app = GUCmailApp(context, gateway=gateway)
        stamp("constructed_ms")
        try:
            async with app.run_test(size=(120, 36)) as pilot:
                table = app.query_one("#message-table", DataTable)
                table.focus()
                async with asyncio.timeout(15):
                    await pilot.pause()
                await pilot.press("tab")
                assert app.focused is not None
                assert app.screen.region.width == 120
                stamp("navigable_shell_ms")
                async with asyncio.timeout(30):
                    while table.row_count == 0 or app.ui.busy or table._require_update_dimensions:
                        await pilot.pause(0.005)
                    await pilot.pause()
                stamp("first_populated_ms")
                assert 0 < table.row_count <= 200
                details = {
                    "stages": stages,
                    "rows": table.row_count,
                    "fixture_messages": gateway.count("mail"),
                    "provider_calls": dict(gateway.counts),
                    "resources": memory(),
                    "size": [120, 36],
                    "rendering": {
                        "no_color_filter": app.no_color,
                        "color_capability": app._terminal_color_capability().value,
                        "active_theme": app.active_theme.id,
                    },
                    "synthetic_transport": True,
                }
        finally:
            context.close()
        stamp("cleanup_finished_ms")
        print(json.dumps(details))

    asyncio.run(run())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=("guard", "baseline", "large"))
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--tui-samples", type=int, default=5)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--only-tui", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args.child)
        return
    if not 1 <= args.samples <= 100 or not 1 <= args.tui_samples <= 100:
        parser.error("Sample counts must be between 1 and 100")
    guard = install_from_env()
    from qa.offline.children import guarded_environment

    report = {
        "method": "Fresh Python processes measured by perf_counter_ns, including guard bootstrap. "
        "Mounted TUI output has explicit shell/populated/cleanup markers. "
        "No OS cache flush; no authentication; actual bounded SQL synthetic provider.",
        "diagnostic_only": args.diagnostic,
        "performance_gate": "Not evaluated during diagnostic/concurrent runs"
        if args.diagnostic
        else "Gross guarded timings; guard-only overhead is reported separately, not subtracted.",
        "complete": False,
        "samples": [],
        "summaries": [],
    }
    source_root = Path(__file__).resolve().parents[1] / "src/gucmail"
    source_hashes = {
        str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    report["source_files"] = source_hashes
    target = guard.output / (
        "startup-diagnostic.json"
        if args.diagnostic
        else "startup-tui-results.json"
        if args.only_tui
        else "startup-results.json"
    )
    for name, arguments, repetitions in (
        ("guard-bootstrap", ["-m", "qa.synthetic.startup", "--child", "guard"], args.samples),
        ("cli-version", ["-m", "gucmail", "version"], args.samples),
        (
            "cli-local-status",
            ["-m", "gucmail", "--json", "--non-interactive", "status"],
            args.samples,
        ),
        (
            "tui-baseline",
            ["-m", "qa.synthetic.startup", "--child", "baseline"],
            args.tui_samples,
        ),
        ("tui-large", ["-m", "qa.synthetic.startup", "--child", "large"], args.tui_samples),
    ):
        if args.only_tui and not name.startswith("tui"):
            continue
        values = []
        for sample in range(repetitions):
            environment = guarded_environment()
            start = time.perf_counter_ns()
            environment["GUCMAIL_AUDIT_LAUNCH_NS"] = str(start)
            try:
                result = subprocess.run(  # noqa: S603 - fixed owned Python modules
                    [sys.executable, *arguments],
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=60,
                    check=True,
                )
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
                report["failure"] = {
                    "operation": name,
                    "sample": sample + 1,
                    "type": type(error).__name__,
                    "stages": str(error.stderr or ""),
                }
                target.write_text(json.dumps(report, indent=2) + "\n")
                raise
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            row = {"operation": name, "sample": sample + 1, "total_through_exit_ms": elapsed}
            if name.startswith("tui") or name == "guard-bootstrap":
                row.update(json.loads(result.stdout))
            else:
                row["output_present"] = bool(result.stdout.strip())
                assert row["output_present"]
            report["samples"].append(row)
            values.append(elapsed)
            target.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "operation": name,
                    "samples": repetitions,
                    "median_through_exit_ms": statistics.median(values),
                }
            ),
            flush=True,
        )
        summary = {"operation": name, "samples": repetitions}
        selected = [row for row in report["samples"] if row["operation"] == name]
        markers = ["total_through_exit_ms"]
        if name.startswith("tui"):
            markers.extend(selected[0]["stages"])
        for marker in markers:
            series = sorted(row.get(marker, row.get("stages", {}).get(marker)) for row in selected)
            summary[marker] = {
                "median": statistics.median(series),
                "p95": series[math.ceil(0.95 * len(series)) - 1],
            }
        report["summaries"].append(summary)
    assert source_hashes == {
        str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }, "Product source changed during startup measurement"
    report["complete"] = True
    target.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
