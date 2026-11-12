from xml.etree import ElementTree

import pytest

from gucmail.platform_scheduler import generate_scheduler_plan, install_scheduler
from gucmail.services.startup_runtime import write_startup_artifact


def test_windows_scheduled_send_artifact_has_matching_unicode_declaration_and_bytes(tmp_path):
    plan = generate_scheduler_plan(
        platform_name="windows",
        windows_user_sid="S-1-5-21-100-200-300-1001",
        output_directory=tmp_path,
        arguments=("python.exe", "-m", "gucmail", "scheduler", "run-once"),
    )
    invoked = []
    install_scheduler(
        plan, dry_run=False, confirmed=True, runner=lambda argv, **kwargs: invoked.append(argv)
    )
    payload = plan.artifacts[0].path.read_bytes()
    assert payload.startswith(b"\xff\xfe")
    assert plan.artifacts[0].path.read_text("utf-16") == plan.artifacts[0].content
    document = ElementTree.fromstring(payload)  # noqa: S314 - locally generated test XML
    assert document.find("{*}Actions/{*}Exec/{*}Command").text == "python.exe"
    assert invoked == list(plan.install_commands)


@pytest.mark.parametrize("content", ["[Service]\nDescription=Synthetic café\n", "plain text"])
def test_non_xml_startup_artifacts_keep_utf8(tmp_path, content):
    path = tmp_path / "synthetic.service"
    write_startup_artifact(path, content, 0o600)
    assert path.read_bytes() == content.encode("utf-8")
