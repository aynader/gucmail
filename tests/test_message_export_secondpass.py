from pathlib import Path

import pytest

from gucmail.errors import ConfirmationRequiredError, ConflictError
from gucmail.services.message_export import MessageExportService


class RawGateway:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def raw_message(self, reference: str) -> bytes:
        self.calls.append(reference)
        return f"Subject: {reference}\r\n\r\nSynthetic\r\n".encode()


def test_raw_message_export_is_exact_confirmation_scoped_and_collision_safe(
    tmp_path: Path,
) -> None:
    gateway = RawGateway()
    service = MessageExportService(gateway)
    plan = service.plan(
        ["ref-one", "ref-two", "ref-one"],
        tmp_path / "export",
        subjects={"ref-one": "Unsafe / first", "ref-two": "Second"},
    )

    assert plan.targets == ("ref-one", "ref-two")
    with pytest.raises(ConfirmationRequiredError):
        service.execute(plan, confirmation_scope=None)
    assert gateway.calls == []

    result = service.execute(plan, confirmation_scope=plan.confirmation_scope)
    paths = tuple(result.results)
    assert gateway.calls == ["ref-one", "ref-two"]
    assert len(paths) == 2
    assert all(isinstance(path, Path) and path.suffix == ".eml" for path in paths)
    assert all(path.parent == (tmp_path / "export").resolve() for path in paths)
    assert all(path.read_bytes().startswith(b"Subject: ref-") for path in paths)

    gateway.calls.clear()
    with pytest.raises(ConflictError):
        service.execute(plan, confirmation_scope=plan.confirmation_scope)
    assert gateway.calls == []


def test_raw_message_export_rolls_back_files_created_before_provider_failure(
    tmp_path: Path,
) -> None:
    class FailingGateway(RawGateway):
        def raw_message(self, reference: str) -> bytes:
            if reference == "bad":
                raise RuntimeError("synthetic provider failure")
            return super().raw_message(reference)

    service = MessageExportService(FailingGateway())
    plan = service.plan(["good", "bad"], tmp_path / "export")

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        service.execute(plan, confirmation_scope=plan.confirmation_scope)

    assert not tuple((tmp_path / "export").glob("*.eml"))
