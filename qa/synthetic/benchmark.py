"""Measured guarded local performance. No coverage instrumentation or live services."""

from __future__ import annotations

import ctypes
import gc
import json
import statistics
import time

from gucmail.services.contacts import ContactQuery, ContactsService
from gucmail.services.drafts import DraftService
from gucmail.services.index import IndexDocument, SearchIndex
from gucmail.services.messages import MessageService
from gucmail.services.tasks import TaskQuery, TaskService
from gucmail.state import StateStore
from qa.offline.guard import install_from_env
from qa.synthetic.datasets import messages, seed
from qa.synthetic.mailbox import LocalMailbox


class Memory(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def memory():
    counters = Memory()
    counters.cb = ctypes.sizeof(counters)
    ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    ctypes.windll.kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    handles = ctypes.c_ulong()
    ctypes.windll.kernel32.GetProcessHandleCount(process, ctypes.byref(handles))
    return {
        "rss_bytes": counters.WorkingSetSize,
        "peak_rss_bytes": counters.PeakWorkingSetSize,
        "handles": handles.value,
    }


def measure(name, operation, *, repetitions=15, metadata=None):
    values = []
    counts = []
    before = memory()
    for _ in range(repetitions):
        start = time.perf_counter()
        result = operation()
        values.append((time.perf_counter() - start) * 1000)
        if hasattr(result, "__len__"):
            counts.append(len(result))
        elif hasattr(result, "items") and not callable(result.items):
            counts.append(len(result.items))
        del result
    ordered = sorted(values)
    gc.collect()
    return {
        "operation": name,
        "repetitions": repetitions,
        "milliseconds": values,
        "median_ms": statistics.median(values),
        "p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "min_ms": min(values),
        "max_ms": max(values),
        "result_counts": sorted(set(counts)),
        "resources_before": before,
        "resources_after_gc": memory(),
        **(metadata or {}),
    }


def main():
    guard = install_from_env()
    rows = []
    for scale in ("baseline", "large", "metadata"):
        root = guard.private / "datasets" / scale
        seed(scale, root)
        gateway = LocalMailbox(root / "mailbox.sqlite")
        service = MessageService(gateway)
        for offset in (0, 500, 50000) if scale != "baseline" else (0, 500):
            rows.append(
                measure(
                    f"{scale}.mail.page.{offset}",
                    lambda offset=offset: service.list_messages("inbox", limit=100, offset=offset),
                )
            )
        rows.append(
            measure(
                f"{scale}.mail.sort.subject",
                lambda: service.list_messages("inbox", limit=100, order_by="subject"),
            )
        )
        if scale == "metadata":
            continue
        contacts = ContactsService(gateway)
        tasks = TaskService(gateway)
        for term in ("Synthetic", "University 00042", "研究"):
            rows.append(
                measure(
                    f"{scale}.contacts.search.{term}",
                    lambda term=term: contacts.search(ContactQuery(text=term), limit=100),
                )
            )
        rows.append(
            measure(
                f"{scale}.tasks.search",
                lambda: tasks.search(TaskQuery(exclude_completed=True), limit=100),
            )
        )
        state = StateStore(root / "profile/data/state.sqlite", account_key="synthetic-demo")
        drafts = DraftService(state, root / "profile/data/recovery")
        rows.append(measure(f"{scale}.drafts.list", drafts.list, repetitions=5))
        rows.append(measure(f"{scale}.schedule.list", state.list_scheduled_jobs, repetitions=5))
        index = SearchIndex(root / "profile/data/index.sqlite")
        # The fixture's path is AppPaths.index_database (not the cache directory).
        assert index.stats().documents == (1000 if scale == "baseline" else 100000)
        for term, options in [
            ("Academic", {}),
            ("optics", {}),
            ("Academic", {"offset": 50000}),
            ("optcs", {"fuzzy": True}),
            ("notes", {"fields": ("attachment_names",)}),
        ]:
            rows.append(
                measure(
                    f"{scale}.index.{term}.{options}",
                    lambda term=term, options=options: index.search(term, limit=100, **options),
                    repetitions=7,
                )
            )
        # Copy the large fixture before measuring index writes; query fixture stays unchanged.
        if scale == "large":
            copy = guard.private / "bench-index.sqlite"
            with index._connection() as db:
                import sqlite3

                target = sqlite3.connect(copy)
                db.backup(target)
                target.close()
            writable = SearchIndex(copy)
            fixtures = [IndexDocument.from_value(item) for item in messages(50, ["/Inbox"])]
            rows.append(
                measure(
                    "large.index.upsert_existing_50",
                    lambda: writable.upsert_many(fixtures),
                    repetitions=3,
                    metadata={
                        "records_per_call": 50,
                        "transport": "actual production SearchIndex.upsert_many",
                    },
                )
            )
        gateway.close()
    report = {
        "method": "perf_counter wall time, 64-bit Windows GetProcessMemoryInfo/GetProcessHandleCount; guarded single process; no coverage. Percentile uses nearest upper sample.",
        "boundary": "Persistent synthetic local transport timing is not EWS/server timing. Index/draft/state operations use production implementations.",
        "results": rows,
        "final_resources": memory(),
    }
    (guard.output / "performance-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"measurements": len(rows), "resources": memory()}))


if __name__ == "__main__":
    main()
