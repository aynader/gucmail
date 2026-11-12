"""Inert spawned-worker fixtures: never import the CLI, UI or test suites here."""

import os
import time
from pathlib import Path


def _slow_extraction_worker(connection, format_name, data, policy):
    del format_name, policy
    try:
        Path(data.decode()).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(30)
    finally:
        connection.close()

