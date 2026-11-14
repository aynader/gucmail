"""Regressions found on Linux with Python 3.12."""

import os
import sys

import pytest

from gucmail.services.contacts import ContactQuery, ContactsService
from gucmail.services.gateway import DeferredGateway, resolve
from gucmail.services.pim import PimPage
from gucmail.services.tasks import TaskQuery, TaskService


class PagedProvider:
    def __init__(self):
        self.calls = []

    def search_contacts_page(self, query, *, folder_reference, limit, offset=0):
        self.calls.append(("contacts", offset))
        return PimPage((), offset, 0, None, "Synthetic page.")

    def search_tasks_page(self, query, *, folder_reference, limit, offset=0):
        self.calls.append(("tasks", offset))
        return PimPage((), offset, 0, None, "Synthetic page.")


def test_resolve_returns_the_provider_behind_a_deferred_wrapper():
    provider = PagedProvider()
    assert resolve(DeferredGateway(lambda: provider)) is provider
    assert resolve(provider) is provider


def test_paging_works_through_the_deferred_wrapper():
    """From Python 3.12, isinstance(proxy, Protocol) ignores __getattr__."""

    provider = PagedProvider()
    deferred = DeferredGateway(lambda: provider)
    ContactsService(deferred).page(ContactQuery(text="x"), limit=5, offset=5)
    TaskService(deferred).page(TaskQuery(), limit=5, offset=5)
    assert provider.calls == [("contacts", 5), ("tasks", 5)]


@pytest.mark.skipif(os.name == "nt", reason="the helper process inherits stderr only on POSIX")
def test_worker_helper_starts_when_stderr_has_no_file_handle(monkeypatch):
    """Textual replaces stderr with an object whose fileno() is -1."""

    from multiprocessing import resource_tracker

    from gucmail.services.extractors import ensure_resource_tracker

    class NoHandle:
        def write(self, text):
            return len(text)

        def flush(self):
            pass

        def fileno(self):
            return -1

    seen = []
    monkeypatch.setattr(
        resource_tracker, "ensure_running", lambda: seen.append(sys.stderr.fileno())
    )
    fake = NoHandle()
    monkeypatch.setattr(sys, "stderr", fake)
    ensure_resource_tracker()
    assert seen and seen[0] >= 0
    assert sys.stderr is fake
