"""Configuration, settings, layouts, cache, quota and offline help."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.markdown import Markdown

from gucmail.cli import core
from gucmail.cli.main import app
from gucmail.constants import MAX_PAGE_SIZE
from gucmail.errors import (
    ConfigurationError,
    ConfirmationRequiredError,
)
from gucmail.formatting import human_bytes
from gucmail.help import HelpRepository, HelpRuntimeContext
from gucmail.help.cli_syntax import CliCommandSyntax
from gucmail.output import OutputFormat
from gucmail.palette import universal_palette_registry
from gucmail.security import sanitize_terminal
from gucmail.services.layout_presets import LayoutPresetService, validate_name
from gucmail.services.quota import QuotaService
from gucmail.settings import SettingLevel, SettingsService

config_app = typer.Typer(help="See or change the settings file.")


settings_app = typer.Typer(help="Find and change settings.")


layouts_app = typer.Typer(help="Save and load window layouts.")


help_app = typer.Typer(help="Read the built-in help, no internet needed.")


cache_app = typer.Typer(help="See or clear the local cache.")


quota_app = typer.Typer(help="How much mailbox space you have left.")


def _help_repository() -> HelpRepository:
    """Load and validate the package-resident corpus without touching the network."""

    return HelpRepository.load_default()


def _installed_cli_help_syntax() -> CliCommandSyntax:
    """Copy the installed parser grammar without invoking commands or converters."""

    from typer.core import TyperCommand, TyperGroup
    from typer.main import get_command

    from gucmail.help.cli_syntax import syntax_from_command

    command = get_command(app)
    if not isinstance(command, (TyperCommand, TyperGroup)):
        raise ValueError("Help grammar requires a Typer command tree.")
    return syntax_from_command(command, eager_exit_parameters=frozenset({"version_requested"}))


def _installed_cli_help_references() -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Return installed leaf and invokable-group paths and their options."""

    from typer.core import TyperGroup
    from typer.main import get_command

    command_ids: set[str] = set()
    cli_paths: set[str] = set()
    cli_options: dict[str, set[str]] = {}
    pending: list[tuple[tuple[str, ...], object]] = [((), get_command(app))]
    while pending:
        prefix, command = pending.pop()
        if prefix and (not isinstance(command, TyperGroup) or command.invoke_without_command):
            command_ids.add(".".join(prefix))
            shell_path = " ".join(prefix)
            cli_paths.add(shell_path)
            options: set[str] = set()
            for parameter in getattr(command, "params", ()):
                options.update(str(value) for value in getattr(parameter, "opts", ()))
                options.update(str(value) for value in getattr(parameter, "secondary_opts", ()))
            cli_options[shell_path] = options
        if not isinstance(command, TyperGroup):
            continue
        for name, child in command.commands.items():
            path = (*prefix, name)
            command_ids.add(".".join(path))
            pending.append((path, child))
    return command_ids, cli_paths, cli_options


def _help_runtime_context(
    ctx: typer.Context,
    *,
    active_view: str | None = None,
) -> HelpRuntimeContext:
    """Build an offline Help context without exposing private setting values."""

    settings: dict[str, object] = {}
    for value in core.settings_service(ctx).values():
        if value.spec.sensitive:
            # Generated Help needs to distinguish configured from absent, never the value.
            settings[value.spec.setting_id] = bool(value.secret_configured)
        else:
            settings[value.spec.setting_id] = value.effective_value
    return HelpRuntimeContext(active_view=active_view, settings=settings)


def _show_help_page(ctx: typer.Context, page_id: str, *, command: str) -> None:
    try:
        rendered = _help_repository().render(page_id, context=_help_runtime_context(ctx))
    except BaseException as exc:
        core.report_error(ctx, exc, command=command)
        return
    state = core.cli_state(ctx)
    if state.output_format is OutputFormat.HUMAN:
        if state.quiet:
            return
        state.writer.console.rule(sanitize_terminal(rendered.page.title))
        state.writer.console.print(Markdown(rendered.markdown))
        return
    state.writer.emit(rendered, command=command, source="packaged_offline_help")


@help_app.callback(invoke_without_command=True)
def help_callback(ctx: typer.Context) -> None:
    """Open the packaged Help home page when no subcommand is supplied."""

    if ctx.invoked_subcommand is None:
        repository = _help_repository()
        _show_help_page(ctx, repository.home_page_id, command="help")


@help_app.command("show")
def help_show(
    ctx: typer.Context,
    page: str = typer.Argument(..., help="Help page ID, alias, or legacy topic ID."),
) -> None:
    """Render one complete Help article entirely offline."""

    _show_help_page(ctx, page, command="help show")


@help_app.command("list")
def help_list(
    ctx: typer.Context,
    category: str | None = typer.Option(None, help="Limit pages to this exact category."),
) -> None:
    """List available packaged Help pages and their stable IDs."""

    def action() -> tuple[dict[str, object], ...]:
        repository = _help_repository()
        pages = repository.pages_in_category(category) if category else repository.pages
        if category and not pages:
            raise ConfigurationError(f"Unknown or empty Help category: {category}")
        return tuple(
            {
                "id": page.page_id,
                "title": page.title,
                "category": page.category,
                "level": page.skill_level.value,
                "summary": page.summary,
            }
            for page in pages
        )

    core.run(ctx, "help list", action, title="Help pages", source="packaged_offline_help")


@help_app.command("categories")
def help_categories(ctx: typer.Context) -> None:
    """List the Help hierarchy's top-level categories."""

    def action() -> tuple[dict[str, object], ...]:
        repository = _help_repository()
        return tuple(
            {
                "category": category,
                "pages": len(repository.pages_in_category(category)),
            }
            for category in repository.categories
        )

    core.run(
        ctx,
        "help categories",
        action,
        title="Help categories",
        source="packaged_offline_help",
    )


@help_app.command("toc")
def help_toc(
    ctx: typer.Context,
    headings: bool = typer.Option(
        False,
        "--headings/--no-headings",
        help="Include article heading anchors below every page.",
    ),
) -> None:
    """Show the complete hierarchical table of contents."""

    def action() -> dict[str, object]:
        repository = _help_repository()
        toc = repository.table_of_contents(
            platform=_help_runtime_context(ctx).platform,
            include_headings=headings,
        )
        return {
            "title": toc.title,
            "categories": tuple(
                {
                    "id": category.node_id,
                    "title": category.title,
                    "pages": tuple(
                        {
                            "id": page.page_id,
                            "title": page.title,
                            "headings": tuple(
                                {
                                    "title": heading.title,
                                    "anchor": heading.anchor,
                                }
                                for heading in page.children
                            ),
                        }
                        for page in category.children
                    ),
                }
                for category in toc.children
            ),
        }

    core.run(
        ctx,
        "help toc",
        action,
        title="Help table of contents",
        source="packaged_offline_help",
    )


@help_app.command("search")
def help_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Question, keywords, or misspelled topic."),
    limit: int = typer.Option(10, min=1, max=50),
) -> None:
    """Run typo-tolerant, natural-language search over the offline Help corpus."""

    if not query.strip():
        raise typer.BadParameter("Help search needs a non-empty question.")
    core.run(
        ctx,
        "help search",
        lambda: _help_repository().search(
            query,
            limit=limit,
            context=_help_runtime_context(ctx),
        ),
        title="Help search results",
        source="packaged_offline_help",
    )


@help_app.command("error")
def help_error(
    ctx: typer.Context,
    message: str = typer.Argument(
        ...,
        help="Sanitized error name or message; do not paste credentials or private mail.",
    ),
    limit: int = typer.Option(3, min=1, max=10),
) -> None:
    """Map a known error or user-visible symptom to recovery articles."""

    if not message.strip():
        raise typer.BadParameter("Error-aware Help needs a non-empty description.")
    core.run(
        ctx,
        "help error",
        lambda: _help_repository().for_error(
            message,
            limit=limit,
            context=_help_runtime_context(ctx, active_view="error"),
        ),
        title="Error-aware Help",
        source="packaged_offline_help",
    )


def _show_named_help_page(ctx: typer.Context, page_id: str, command: str) -> None:
    _show_help_page(ctx, page_id, command=command)


@help_app.command("commands")
def help_commands(ctx: typer.Context) -> None:
    """Show the generated reference for every canonical command."""

    _show_named_help_page(ctx, "reference.commands", "help commands")


@help_app.command("shortcuts")
def help_shortcuts(ctx: typer.Context) -> None:
    """Show keyboard navigation and current default shortcuts."""

    _show_named_help_page(ctx, "reference.shortcuts", "help shortcuts")


@help_app.command("glossary")
def help_glossary(ctx: typer.Context) -> None:
    """Show the offline GUC Mail and Exchange glossary."""

    _show_named_help_page(ctx, "glossary", "help glossary")


@help_app.command("validate")
def help_validate(ctx: typer.Context) -> None:
    """Validate packaged pages, metadata, and internal links offline."""

    def action() -> dict[str, object]:
        repository = _help_repository()
        registry = universal_palette_registry()
        from gucmail.help.reference import DOCUMENTED_NLP_EXAMPLES, shortcut_records
        from gucmail.nlp import load_command_corpus

        cli_command_ids, cli_paths, cli_options = _installed_cli_help_references()
        known_commands = {command.command_id for command in registry.all()} | cli_command_ids
        known_commands.update(
            " ".join(command.cli_path) for command in registry.all() if command.cli_path is not None
        )
        setting_specs = tuple(core.settings_service(ctx).registry)
        known_settings = {spec.setting_id for spec in setting_specs}
        known_settings.update(spec.attr_name for spec in setting_specs if spec.attr_name)
        corpus = load_command_corpus()
        report = repository.validate(
            known_command_ids=known_commands,
            known_setting_ids=known_settings,
            known_shortcut_ids={record.shortcut_id for record in shortcut_records()},
            known_nlp_examples={
                *(phrase for group in corpus.groups for phrase in group.phrases),
                *(example.text for example in DOCUMENTED_NLP_EXAMPLES),
            },
            known_cli_paths=cli_paths,
            known_cli_options=cli_options,
            known_cli_syntax=_installed_cli_help_syntax(),
        )
        return {
            "valid": report.ok,
            "pages": len(repository.pages),
            "categories": len(repository.categories),
            "errors": report.errors,
            "warnings": report.warnings,
        }

    core.run(
        ctx,
        "help validate",
        action,
        title="Help validation",
        source="packaged_offline_help",
    )


def _resolve_setting_id(service: SettingsService, value: str) -> str:
    try:
        return service.registry.get(value).setting_id
    except ConfigurationError as original:
        try:
            return service.registry.for_app_config_field(value).setting_id
        except ConfigurationError:
            raise original from None


def _setting_record(value: Any) -> dict[str, object]:
    configured = value.configured_value
    effective = value.effective_value
    if value.spec.attr_name in {"email", "username"}:
        configured = "<configured>" if configured else None
        effective = "<configured>" if effective else None
    return {
        "id": value.spec.setting_id,
        "label": value.spec.label,
        "category": value.spec.category,
        "configured": configured,
        "effective": effective,
        "source": value.source.value,
        "overridden": value.overridden,
        "environment_variable": value.environment_variable,
        "level": value.spec.level.value,
        "apply": value.spec.apply_semantics.value,
        "help": value.spec.help_id,
    }


@layouts_app.command("list")
def layouts_list(ctx: typer.Context) -> None:
    """List local saved layout names and display preferences; never connect to Exchange."""
    core.run(
        ctx,
        "layouts list",
        lambda: LayoutPresetService(core.settings_service(ctx)).catalog().presets,
        title="Saved layouts",
        source="local",
    )


@layouts_app.command("save")
def layouts_save(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    replace_existing: bool = typer.Option(
        False, "--replace", help="Explicitly replace an existing name."
    ),
    dry_run: bool = typer.Option(False, help="Preview without saving."),
) -> None:
    """Save the configured layout. TUI Save captures its displayed arrangement."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(core.settings_service(ctx))
        catalog = service.catalog()
        selected = validate_name(name)
        exists = any(key.casefold() == selected.casefold() for key in catalog.presets)
        if exists and not replace_existing:
            raise ConfirmationRequiredError("This layout name exists. Use --replace to replace it.")
        if not dry_run:
            service.save(
                selected, catalog.current, catalog=catalog, replace_existing=replace_existing
            )
        return {
            "name": selected,
            "settings": catalog.current,
            "dry_run": dry_run,
            "replaced": exists,
        }

    core.run(
        ctx,
        "layouts save",
        action,
        title="Layout preview" if dry_run else "Layout saved",
        source="local",
    )


@layouts_app.command("load")
def layouts_load(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview the saved choices without applying them."),
) -> None:
    """Atomically apply one named layout through Settings; mailbox data is unaffected."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(core.settings_service(ctx))
        catalog = service.catalog()
        preview = service.preview(name, catalog=catalog)
        if not dry_run:
            service.load(name, catalog=catalog)
            core.cli_state(ctx).context.reload_config()
        return {**preview, "dry_run": dry_run}

    core.run(
        ctx,
        "layouts load",
        action,
        title="Layout preview" if dry_run else "Layout loaded",
        source="local",
    )


@layouts_app.command("delete")
def layouts_delete(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview removal without deleting the preset."),
) -> None:
    """Remove a saved layout with confirmation; keep the active arrangement."""

    def action() -> dict[str, Any]:
        service = LayoutPresetService(core.settings_service(ctx))
        catalog = service.catalog()
        preview = service.preview(name, catalog=catalog)
        if not dry_run:
            core.confirm(
                ctx, f"Remove saved layout {preview['name']}?", "layout-delete:" + preview["name"]
            )
            service.delete(name, catalog=catalog)
        return {"name": preview["name"], "dry_run": dry_run, "removed": not dry_run}

    core.run(ctx, "layouts delete", action, title="Layout removal", source="local")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show non-secret configuration with account identifiers redacted."""

    core.run(
        ctx,
        "config show",
        lambda: core.cli_state(ctx).context.config.redacted_dict(),
        title="Configuration",
        source="local",
    )


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: str = typer.Argument(...),
    value: str = typer.Argument(...),
) -> None:
    """Set one supported non-secret configuration value."""

    def action() -> dict[str, object]:
        context = core.cli_state(ctx).context
        service = core.settings_service(ctx)
        setting_id = _resolve_setting_id(service, key)
        revision = service.set(setting_id, value, confirm_external=False)
        context.reload_config()
        return {
            "setting": _setting_record(service.value(setting_id)),
            "revision": revision.revision_id if revision is not None else None,
        }

    core.run(ctx, "config set", action, title="Configuration updated", source="local")


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Show platform-specific configuration and state paths."""

    def action() -> dict[str, str]:
        paths = core.cli_state(ctx).context.paths
        return {
            "config": str(paths.config_file),
            "state": str(paths.state_database),
            "index": str(paths.index_database),
            "backups": str(paths.backup_dir),
            "logs": str(paths.log_dir),
        }

    core.run(ctx, "config path", action, title="Paths", source="local")


@settings_app.command("list")
def settings_list(
    ctx: typer.Context,
    category: str | None = typer.Option(None, help="Exact settings category."),
    level: SettingLevel = typer.Option(SettingLevel.EXPERT, case_sensitive=False),
    limit: int = typer.Option(500, min=1, max=1_000),
) -> None:
    """List effective settings, their source, and override state."""

    def action() -> tuple[dict[str, object], ...]:
        service = core.settings_service(ctx)
        values = service.values(category=category)
        if category and not values:
            raise ConfigurationError(f"Unknown settings category: {category}")
        ranks = {
            SettingLevel.BASIC: 0,
            SettingLevel.ADVANCED: 1,
            SettingLevel.EXPERT: 2,
        }
        return tuple(
            _setting_record(value) for value in values if ranks[value.spec.level] <= ranks[level]
        )[:limit]

    core.run(ctx, "settings list", action, title="Settings", source="local")


@settings_app.command("categories")
def settings_categories(ctx: typer.Context) -> None:
    """List settings categories and entry counts."""

    def action() -> tuple[dict[str, object], ...]:
        service = core.settings_service(ctx)
        categories = tuple(dict.fromkeys(spec.category for spec in service.registry))
        return tuple(
            {
                "category": category,
                "settings": len(service.registry.by_category(category)),
            }
            for category in categories
        )

    core.run(ctx, "settings categories", action, title="Settings categories", source="local")


@settings_app.command("get")
def settings_get(ctx: typer.Context, setting: str = typer.Argument(...)) -> None:
    """Show one setting with configured/effective value and precedence source."""

    def action() -> dict[str, object]:
        service = core.settings_service(ctx)
        return _setting_record(service.value(_resolve_setting_id(service, setting)))

    core.run(ctx, "settings get", action, title="Setting", source="local")


@settings_app.command("search")
def settings_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(20, min=1, max=100),
    level: SettingLevel = typer.Option(SettingLevel.EXPERT, case_sensitive=False),
) -> None:
    """Search labels, IDs, descriptions, categories, and aliases."""

    if not query.strip():
        raise typer.BadParameter("Settings search needs non-empty text.")

    def action() -> tuple[dict[str, object], ...]:
        service = core.settings_service(ctx)
        return tuple(
            {
                "id": result.spec.setting_id,
                "label": result.spec.label,
                "category": result.spec.category,
                "description": result.spec.description,
                "score": round(result.score, 2),
                "matched": result.matched_text,
                "help": result.spec.help_id,
            }
            for result in service.search(query, limit=limit, maximum_level=level)
        )

    core.run(ctx, "settings search", action, title="Settings search", source="local")


def _edit_setting(
    ctx: typer.Context,
    *,
    setting: str,
    value: str | None,
    reset: bool,
    dry_run: bool,
) -> None:
    def action() -> dict[str, object]:
        context = core.cli_state(ctx).context
        service = core.settings_service(ctx)
        setting_id = _resolve_setting_id(service, setting)
        session = service.begin()
        if reset:
            session.reset(setting_id)
        else:
            assert value is not None
            session.set(setting_id, value)
        preview = session.preview()
        if dry_run or not preview:
            session.cancel()
            return {"dry_run": dry_run, "changes": preview, "applied": False}
        external = any(change.external_effect for change in preview)
        if external:
            core.confirm(
                ctx,
                "Apply setting changes that affect an external integration?",
                "settings-external:" + ",".join(change.setting_id for change in preview),
            )
        revision = session.apply(
            reason=f"CLI {'reset' if reset else 'update'}: {setting_id}",
            confirm_external=external,
        )
        context.reload_config()
        return {
            "dry_run": False,
            "changes": preview,
            "applied": revision is not None,
            "revision": revision.revision_id if revision is not None else None,
            "setting": _setting_record(service.value(setting_id)),
        }

    command = "settings reset" if reset else "settings set"
    core.run(ctx, command, action, title=command.title(), source="local")


@settings_app.command("set")
def settings_set(
    ctx: typer.Context,
    setting: str = typer.Argument(...),
    value: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Validate and preview without saving."),
) -> None:
    """Validate, preview, and apply one non-secret setting."""

    _edit_setting(ctx, setting=setting, value=value, reset=False, dry_run=dry_run)


@settings_app.command("reset")
def settings_reset(
    ctx: typer.Context,
    setting: str = typer.Argument(...),
    dry_run: bool = typer.Option(False, help="Preview the default restoration."),
) -> None:
    """Reset one setting to its built-in default."""

    _edit_setting(ctx, setting=setting, value=None, reset=True, dry_run=dry_run)


@settings_app.command("export")
def settings_export(
    ctx: typer.Context,
    output: Path | None = typer.Option(None, help="Optional JSON destination."),
    category: list[str] = typer.Option([], "--category"),
    setting: list[str] = typer.Option([], "--setting"),
    overwrite: bool = typer.Option(False),
) -> None:
    """Export portable non-secret settings; secret values are always excluded."""

    def action() -> dict[str, object]:
        payload = core.settings_service(ctx).export(
            categories=category or None,
            setting_ids=setting or None,
        )
        if output is None:
            return payload
        target = output.expanduser().resolve(strict=False)
        if target.exists() and not overwrite:
            raise ConfirmationRequiredError(
                "The settings export destination exists; use --overwrite and --yes."
            )
        if target.exists():
            core.confirm(ctx, f"Overwrite settings export {target}?", f"settings-export:{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "path": str(target),
            "settings": len(payload["settings"]),
            "excluded_secrets": payload["excluded_secrets"],
        }

    core.run(ctx, "settings export", action, title="Settings export", source="local")


@settings_app.command("import")
def settings_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, file_okay=True, readable=True),
    apply: bool = typer.Option(False, help="Apply the previewed changes after confirmation."),
) -> None:
    """Preview a JSON settings import; applying is explicit and revisioned."""

    def action() -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Could not read settings import: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("Settings import must be a JSON object.")
        context = core.cli_state(ctx).context
        service = core.settings_service(ctx)
        plan = service.plan_import(payload)
        if not apply or not plan.changes:
            return {"plan": plan, "applied": False}
        core.confirm(
            ctx,
            f"Apply exactly {len(plan.changes)} imported setting change(s)?",
            "settings-import:" + ",".join(change.setting_id for change in plan.changes),
        )
        revision = service.apply_import(plan, confirm_external=True)
        context.reload_config()
        return {
            "plan": plan,
            "applied": revision is not None,
            "revision": revision.revision_id if revision is not None else None,
        }

    core.run(ctx, "settings import", action, title="Settings import", source="local")


@settings_app.command("history")
def settings_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, min=1, max=100),
) -> None:
    """List bounded non-secret settings revisions."""

    core.run(
        ctx,
        "settings history",
        lambda: core.settings_service(ctx).revisions.list()[:limit],
        title="Settings history",
        source="local",
    )


@settings_app.command("undo")
def settings_undo(
    ctx: typer.Context,
    revision: str | None = typer.Argument(None, help="Revision ID; defaults to latest."),
) -> None:
    """Restore values from one previous revision after confirmation."""

    def action() -> dict[str, object]:
        context = core.cli_state(ctx).context
        core.confirm(
            ctx,
            "Restore settings from the selected revision?",
            f"settings-undo:{revision or 'latest'}",
        )
        restored = core.settings_service(ctx).undo(revision)
        context.reload_config()
        return {
            "restored": restored is not None,
            "revision": restored.revision_id if restored is not None else None,
        }

    core.run(ctx, "settings undo", action, title="Settings restored", source="local")


@cache_app.command("status")
def cache_status(ctx: typer.Context) -> None:
    """Show whether the local cache is on, where it lives and whether it is healthy."""

    def summary(result: dict[str, Any]) -> dict[str, str]:
        health = result["state"]
        healthy = health.get("integrity") == "ok" and health.get("foreign_key_errors", 0) == 0
        return {
            "Cache": "On" if result["enabled"] else "Off",
            "Folder": result["directory"],
            "Local database": f"{'Healthy' if healthy else 'Damaged - run gucmail doctor'}, "
            f"{human_bytes(health.get('size_bytes'))}",
        }

    core.run(
        ctx,
        "cache status",
        lambda: {
            "enabled": core.cli_state(ctx).context.config.cache_enabled,
            "directory": str(core.cli_state(ctx).context.paths.cache_dir),
            "state": core.cli_state(ctx).context.state.health(),
        },
        title="Cache",
        source="local",
        human_data=summary,
    )


@cache_app.command("clear")
def cache_clear(ctx: typer.Context, dry_run: bool = typer.Option(False)) -> None:
    """Clear disposable cached references and checkpoints, never mailbox or backup data."""

    def action() -> Any:
        if dry_run:
            return {"dry_run": True, "scope": "disposable cache tables only"}
        core.confirm(ctx, "Clear disposable local cache tables?", "cache-clear:v1")
        return core.cli_state(ctx).context.state.clear_cache()

    core.run(ctx, "cache clear", action, title="Cache clear", source="local")


@quota_app.callback(invoke_without_command=True)
def quota_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit the quota report as JSON."),
) -> None:
    """Show server quota, configured evidence, and clearly labelled GUC fallback data."""

    if json_output:
        core.cli_state(ctx).output_format = OutputFormat.JSON
    if ctx.invoked_subcommand is None:
        core.run(ctx, "quota", lambda: core.quota_action(ctx), title="Mailbox and local capacity")


@quota_app.command("show")
def quota_show(ctx: typer.Context) -> None:
    """Show Exchange quota evidence and separate local capacity."""

    core.run(ctx, "quota show", lambda: core.quota_action(ctx), title="Mailbox and local capacity")


@quota_app.command("folders")
def quota_folders(
    ctx: typer.Context,
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Show folder item counts; never mislabel them as byte usage or quota."""

    def action() -> dict[str, Any]:
        # The EWS gateway accepts at most MAX_PAGE_SIZE folders per request; the
        # default --limit 500 used to fail live with "limit must be between 1 and 200".
        gateway = core.gateway(ctx)
        folders = list(gateway.list_folders(limit=min(limit, MAX_PAGE_SIZE)))
        exhausted = len(folders) < min(limit, MAX_PAGE_SIZE)
        while not exhausted and len(folders) < limit:
            request = min(MAX_PAGE_SIZE, limit - len(folders))
            page = list(gateway.list_folders(limit=request, offset=len(folders)))[:request]
            folders.extend(page)
            exhausted = len(page) < request
        return {
            "folders": tuple(folders),
            "notice": (
                "Standard EWS folder metadata exposes item counts here, not exact folder bytes. "
                "These values are not mailbox quota."
            ),
            "exact_folder_sizes_available": False,
        }

    core.run(ctx, "quota folders", action, title="Folder capacity metadata")


@quota_app.command("local")
def quota_local(ctx: typer.Context) -> None:
    """Show local filesystem capacity without opening an Exchange session."""

    core.run(
        ctx,
        "quota local",
        lambda: QuotaService.local_disk_usage(core.local_capacity_paths(ctx)),
        title="Free space where gucmail keeps its files",
        source="local",
        human_data=_local_capacity_rows,
    )


_CAPACITY_LABELS = {
    "data": "App data",
    "cache": "Cache",
    "search_index": "Search index",
    "backups": "Backups",
    "downloads": "Downloads",
}


def _local_capacity_rows(reports: Any) -> list[dict[str, str]]:
    rows = []
    for report in reports:
        if not report.available:
            free = report.error or "Unknown"
        else:
            free = f"{human_bytes(report.free_bytes)} of {human_bytes(report.total_bytes)}"
        rows.append(
            {
                "Used for": _CAPACITY_LABELS.get(report.label, report.label),
                "Free space": free,
                "Folder": str(report.path),
            }
        )
    return rows
