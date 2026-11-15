"""Run the test suite in parallel inside the offline guard, then clean up.

    python qa/test.py                 # whole suite
    python qa/test.py tests/test_cli.py -k reply

Everything the run writes goes to one temporary folder that is deleted afterwards.
The last lines printed are always a summary, the failed tests and any guard blocks.
Use --keep to keep the folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _summary(private: Path, seconds: float) -> list[str]:
    progress = private / "test-progress.jsonl"
    if not progress.is_file():
        return []
    outcomes: dict[str, str] = {}
    for line in progress.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        # A setup or teardown failure counts against the test too.
        counts = record["phase"] == "call" or record["outcome"] != "passed"
        if counts and outcomes.get(record["test"]) != "failed":
            outcomes[record["test"]] = record["outcome"]
    failed = sorted(test for test, outcome in outcomes.items() if outcome == "failed")
    passed = sum(outcome == "passed" for outcome in outcomes.values())
    skipped = sum(outcome == "skipped" for outcome in outcomes.values())
    # A guard block fails the run even when every test passed (a test caught the error).
    blocked = sorted(
        {
            f"BLOCKED {event.get('operation')} in {event.get('test') or 'unknown test'}"
            for path in private.glob("guard-*.jsonl")
            for event in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if not event.get("expected")
        }
    )
    lines = [
        f"SUMMARY {passed} passed, {len(failed)} failed, {skipped} skipped, "
        f"{len(blocked)} blocked in {seconds:.0f}s"
    ]
    lines += [f"FAILED {test}" for test in failed]
    lines += blocked
    return lines


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    keep = "--keep" in argv
    args = [arg for arg in argv if arg != "--keep"]
    if not any(arg == "-n" or arg.startswith("-n") or arg == "--numprocesses" for arg in args):
        workers = max(2, min(12, (os.cpu_count() or 4) // 2))
        args = ["-n", str(workers), "-q", "--color=no", *args]
    work = Path(tempfile.mkdtemp(prefix="gucmail-tests-"))
    started = time.monotonic()
    try:
        command = [
            sys.executable,
            str(REPO / "qa" / "offline" / "run.py"),
            "--fixture-credentials",
            "--private",
            str(work / "private"),
            "--output",
            str(work / "output"),
            "pytest",
            *args,
        ]
        # Typer forces colored help when any of these is set (GitHub Actions sets the first).
        # Color codes split option names like "--all-folders", so tests see plain text only.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"}
        }
        status = subprocess.call(command, cwd=REPO, env=env)  # noqa: S603 - fixed argv, no shell
        failures = work / "private" / "test-failures.txt"
        if status and failures.is_file():
            print("\n--- failure details ---")
            print(failures.read_text(encoding="utf-8", errors="replace")[-20000:])
        print("\n".join(_summary(work / "private", time.monotonic() - started)))
        return status
    finally:
        if keep:
            print(f"Kept test output in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
