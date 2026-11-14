from __future__ import annotations

import json
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

import pytest
import typer
from typer.core import TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from gucmail.cli import app
from gucmail.cli.settings import _installed_cli_help_syntax
from gucmail.help import HelpRepository
from gucmail.help.cli_syntax import CliCommandSyntax, CliParameterSyntax, syntax_from_command
from gucmail.help.validation import validate_help_pages


@pytest.fixture(scope="module")
def grammar() -> CliCommandSyntax:
    return _installed_cli_help_syntax()


def _raw_parser_accepts(command, tokens, parent=None):
    """Independent installed parser oracle; no conversion/default/callback phase."""

    context = typer.Context(command, parent=parent, **command.context_settings)
    try:
        values, remaining, _ = command.make_parser(context).parse_args(list(tokens))
    except Exception as exc:
        # Parser usage failures only; a trapped callback/converter must escape.
        assert type(exc).__module__.startswith(("click", "typer._click")), repr(exc)
        return False
    help_option = command.get_help_option(context)
    if help_option and values.get(help_option.name):
        return True
    if parent is None and values.get("version_requested"):
        return True
    for parameter in command.get_params(context):
        if parameter.required and parameter.value_is_missing(values.get(parameter.name)):
            return False
    if isinstance(command, TyperGroup):
        if not remaining:
            return command.invoke_without_command
        child = command.commands.get(remaining[0])
        return child is not None and _raw_parser_accepts(child, remaining[1:], context)
    return not remaining or context.allow_extra_args


@pytest.mark.parametrize(
    "example,argv,valid",
    [
        ("--json version", ["--json", "version"], True),
        ("version --json", ["version", "--json"], False),
        ("--ndjson -qv version", ["--ndjson", "-qv", "version"], True),
        ("-qvy version", ["-qvy", "version"], True),
        ("-qvz version", ["-qvz", "version"], False),
        ("--json=true version", ["--json=true", "version"], False),
        (
            "--config 'C:\\Example Folder\\config.toml' version",
            ["--config", "C:\\Example Folder\\config.toml", "version"],
            True,
        ),
        (
            "--credentials-file missing.env version",
            ["--credentials-file", "missing.env", "version"],
            True,
        ),
        ("--config=missing.toml version", ["--config=missing.toml", "version"], True),
        ("--config", ["--config"], False),
        ("--config version", ["--config", "version"], False),
        ("--credentials-file", ["--credentials-file"], False),
        ("version --config missing.toml", ["version", "--config", "missing.toml"], False),
        ("notifications --json rules test", ["notifications", "--json", "rules", "test"], False),
        (
            "--json notifications rules test RULE --profile PROFILE",
            ["--json", "notifications", "rules", "test", "RULE", "--profile", "PROFILE"],
            True,
        ),
        (
            "notifications rules test --recent --limit 5",
            ["notifications", "rules", "test", "--recent", "--limit", "5"],
            True,
        ),
        ("notifications rules test --limit", ["notifications", "rules", "test", "--limit"], False),
        (
            "notifications rules test --profile",
            ["notifications", "rules", "test", "--profile"],
            False,
        ),
        (
            "notifications rules test RULE EXTRA",
            ["notifications", "rules", "test", "RULE", "EXTRA"],
            False,
        ),
        ("--help unknown-child", ["--help", "unknown-child"], True),
        ("--version unknown-child", ["--version", "unknown-child"], True),
        ("--help --unknown", ["--help", "--unknown"], False),
        ("--version --config", ["--version", "--config"], False),
        ("unknown-child --help", ["unknown-child", "--help"], False),
        ("messages --help unknown-child", ["messages", "--help", "unknown-child"], True),
        ("messages show --help", ["messages", "show", "--help"], True),
        ("messages show -h", ["messages", "show", "-h"], True),
        ("messages show --help --unknown", ["messages", "show", "--help", "--unknown"], False),
        ("messages show", ["messages", "show"], False),
        ("messages show REF", ["messages", "show", "REF"], True),
        ("messages show REF EXTRA", ["messages", "show", "REF", "EXTRA"], False),
        ("messages show -- --json", ["messages", "show", "--", "--json"], True),
        ("-- messages show REF", ["--", "messages", "show", "REF"], True),
        ("-- --json version", ["--", "--json", "version"], False),
        ("messages show '--json'", ["messages", "show", "--json"], False),
        (
            "send --subject '--json' --body 'command group --unknown'",
            ["send", "--subject", "--json", "--body", "command group --unknown"],
            True,
        ),
        ("send --subject=--json", ["send", "--subject=--json"], True),
        ("send --subject", ["send", "--subject"], False),
        ("send --body ''", ["send", "--body", ""], True),
        ("help search 'command'", ["help", "search", "command"], True),
        ("help search 'group' --unknown", ["help", "search", "group", "--unknown"], False),
        ("help search 'a # comment'", ["help", "search", "a # comment"], True),
        ('help search "mail not loading"', ["help", "search", "mail not loading"], True),
        ("help search one two", ["help", "search", "one", "two"], False),
        ("help search", ["help", "search"], False),
        ("calendar", ["calendar"], True),
        ("messages", ["messages"], False),
        ("calendar unknown-child", ["calendar", "unknown-child"], False),
        ("calendar agenda --days 14", ["calendar", "agenda", "--days", "14"], True),
        ("calendar --days 14 agenda", ["calendar", "--days", "14", "agenda"], False),
        ("tasks create --subject Final", ["tasks", "create", "--subject", "Final"], True),
        ("tasks create", ["tasks", "create"], False),
        ("tasks create --help", ["tasks", "create", "--help"], True),
        (
            "backup export ARCHIVE --output 'C:\\Folder With Spaces\\result.json'",
            ["backup", "export", "ARCHIVE", "--output", "C:\\Folder With Spaces\\result.json"],
            True,
        ),
        ("backup export ARCHIVE", ["backup", "export", "ARCHIVE"], False),
        (
            "backup export --output result.json",
            ["backup", "export", "--output", "result.json"],
            False,
        ),
        (
            "messages delete ONE TWO --dry-run",
            ["messages", "delete", "ONE", "TWO", "--dry-run"],
            True,
        ),
        ("messages delete --dry-run", ["messages", "delete", "--dry-run"], False),
        ("login --no-check", ["login", "--no-check"], True),
        ("login --no-check=true", ["login", "--no-check=true"], False),
        ("Version", ["Version"], False),
        ("--JSON version", ["--JSON", "version"], False),
    ],
)
def test_grammar_matches_installed_raw_parser_without_running_commands(
    grammar, example, argv, valid
):
    assert _raw_parser_accepts(get_command(app), argv) is valid
    assert (grammar.validate(example) is None) is valid, grammar.validate(example)


def test_grammar_export_and_validation_never_call_callbacks_converters_or_files(monkeypatch):
    command = get_command(app)
    repository = HelpRepository.load_default()

    def forbidden(*args, **kwargs):
        raise AssertionError("Help validation executed a callback, converter or file read")

    pending = [command]
    while pending:
        current = pending.pop()
        monkeypatch.setattr(current, "callback", forbidden)
        context = typer.Context(current)
        for parameter in current.get_params(context):
            monkeypatch.setattr(parameter, "callback", forbidden)
            monkeypatch.setattr(parameter.type, "convert", forbidden)
        if isinstance(current, TyperGroup):
            pending.extend(current.commands.values())
    monkeypatch.setattr(Path, "read_text", forbidden)
    grammar = syntax_from_command(command, eager_exit_parameters=frozenset({"version_requested"}))
    report = repository.validate(known_cli_syntax=grammar)
    assert report.ok, report.errors
    assert grammar.validate("--credentials-file missing.env login --no-check") is None
    assert grammar.validate("--config missing.toml send --to nobody@example.invalid") is None

    def assert_inert(value):
        if is_dataclass(value):
            for field in fields(value):
                assert_inert(getattr(value, field.name))
        elif isinstance(value, tuple):
            for child in value:
                assert_inert(child)
        else:
            assert isinstance(value, (str, bool, int))

    assert_inert(grammar)


@pytest.mark.parametrize("example", ["send --subject 'unclosed", 'help search "unclosed'])
def test_unclosed_quotes_have_a_grammar_error(grammar, example):
    assert grammar.validate(example) == "Unclosed quote in CLI example."


@pytest.mark.parametrize(
    "example,valid",
    [
        ("--json version", True),
        ("version --json", False),
        ("GROUP --help", True),
        ("GROUP COMMAND --help", True),
        ("help search command --unknown", False),
        ("help search group --unknown", False),
        ("messages show", False),
        ("messages show MESSAGE_REF", True),
    ],
)
def test_repository_uses_grammar_and_limits_symbolic_templates(grammar, example, valid):
    repository = HelpRepository.load_default()
    page = repository.get("help.using-help")
    changed = replace(page, body=page.body + f"\n\n## Example\n\n`gucmail {example}`")
    report = validate_help_pages(
        [changed],
        home_page_id=page.page_id,
        known_help_ids={p.page_id for p in repository.pages},
        known_cli_syntax=grammar,
    )
    syntax_errors = [issue for issue in report.errors if issue.code == "invalid_cli_syntax"]
    assert not syntax_errors if valid else syntax_errors


def test_multi_value_and_attached_short_options():
    grammar = CliCommandSyntax(
        "sample",
        (
            CliParameterSyntax("quiet", ("-q",), 0),
            CliParameterSyntax("pair", ("-p", "--pair"), 2, required=True),
            CliParameterSyntax("reference", required=True),
        ),
    )
    assert grammar.validate("-qpfirst second REF") is None
    assert grammar.validate("--pair=first second REF") is None
    assert grammar.validate("--pair first REF") is not None
    assert grammar.validate("-q REF") is not None


def test_harmless_real_version_invocations_confirm_global_scope(tmp_path, grammar):
    environment = {
        f"GUCMAIL_{name}_DIR": str(tmp_path / name.lower())
        for name in ("CONFIG", "DATA", "CACHE", "LOG")
    }
    for example, argv, expected in [
        ("--json version", ["--json", "version"], 0),
        ("version --json", ["version", "--json"], 2),
    ]:
        result = CliRunner().invoke(app, argv, env=environment)
        assert result.exit_code == expected, result.output
        assert (grammar.validate(example) is None) is (expected == 0)
        if expected == 0:
            assert json.loads(result.output)["command"] == "version"


def test_every_installed_command_help_is_accepted_without_execution(grammar):
    command = get_command(app)
    pending = [((), command, None)]
    checked = 0
    while pending:
        prefix, current, parent = pending.pop()
        context = typer.Context(current, parent=parent, **current.context_settings)
        for flag in current.get_help_option(context).opts:
            argv = [*prefix, flag]
            assert _raw_parser_accepts(command, argv)
            assert grammar.validate(" ".join(argv)) is None, argv
            checked += 1
        if isinstance(current, TyperGroup):
            pending.extend(
                ((*prefix, name), child, context) for name, child in current.commands.items()
            )
    assert checked >= 600


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 35), (80, 24), (70, 20)])
async def test_normal_help_search_opens_corrected_examples_and_returns(tmp_path, monkeypatch, size):
    from tests.test_tui import context_for
    from textual.widgets import Input, Markdown

    from gucmail.tui.app import GUCmailApp, HelpScreen

    context = context_for(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline Help attempted to resolve live credentials")

    monkeypatch.setattr(context.credential_resolver, "resolve", forbidden)
    application = GUCmailApp(context, offline=True)
    async with application.run_test(size=size) as pilot:
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(application.screen, HelpScreen)
        for page_id, example in [
            ("settings.overview", "gucmail config set KEY VALUE"),
            ("troubleshooting.backup-search", "gucmail backup inspect BACKUP_ID"),
            ("help.using-help", "gucmail --json help validate"),
        ]:
            screen = application.screen
            page = screen.repository.get(page_id)
            await pilot.press("ctrl+f")
            search = screen.query_one("#help-search", Input)
            assert application.focused is search
            search.value = page.title
            for _ in range(30):
                await pilot.pause(0.025)
                if screen._result_page_ids and screen._result_page_ids[0] == page_id:
                    break
            assert screen._result_page_ids[0] == page_id
            await pilot.press("enter")
            await pilot.pause()
            assert screen.navigator.current.page_id == page_id
            assert example in screen.query_one("#help-article", Markdown)._markdown
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(application.screen, HelpScreen)
    assert context.state.load_help_state().recent_page_ids[0] == "help.using-help"
