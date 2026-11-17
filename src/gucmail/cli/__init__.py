"""The gucmail command line. Each area of commands lives in its own module."""

import typer

from gucmail.cli import core
from gucmail.cli.background import headless_app, startup_app
from gucmail.cli.backup import backup_app
from gucmail.cli.calendar import calendar_app
from gucmail.cli.core import NaturalOrderGroup
from gucmail.cli.exchange import automatic_replies_app, ews_app, inbox_rules_app
from gucmail.cli.mail import attachments_app, folders_app, messages_app, saved_searches_app
from gucmail.cli.main import app
from gucmail.cli.notifications import notifications_app, webhooks_app
from gucmail.cli.outbox import drafts_app, recurring_app, scheduled_app, scheduler_app
from gucmail.cli.people import contact_groups_app, contacts_app, directory_app, recipients_app
from gucmail.cli.search import (
    cleanup_app,
    clusters_app,
    index_app,
    insights_app,
    ocr_app,
    semantic_app,
)
from gucmail.cli.settings import (
    cache_app,
    config_app,
    help_app,
    layouts_app,
    quota_app,
    settings_app,
)
from gucmail.cli.tasks import tasks_app

# Everyday commands stay in `gucmail --help`. These live under `gucmail advanced` and
# keep working at the top level for scripts.
ADVANCED = frozenset(
    {
        "saved-searches",
        "webhooks",
        "config",
        "layouts",
        "cache",
        "ews",
        "automatic-replies",
        "inbox-rules",
        "contact-groups",
        "directory",
        "recipients",
        "scheduler",
        "headless",
        "startup",
        "recurring",
        "index",
        "ocr",
        "semantic",
        "clusters",
        "insights",
    }
)
advanced_app = typer.Typer(
    help="More commands, for power users and scripts.",
    no_args_is_help=True,
    cls=NaturalOrderGroup,
)

for name, group in (
    ("folders", folders_app),
    ("messages", messages_app),
    ("drafts", drafts_app),
    ("attachments", attachments_app),
    ("saved-searches", saved_searches_app),
    ("notifications", notifications_app),
    ("webhooks", webhooks_app),
    ("config", config_app),
    ("settings", settings_app),
    ("layouts", layouts_app),
    ("help", help_app),
    ("cache", cache_app),
    ("quota", quota_app),
    ("ews", ews_app),
    ("calendar", calendar_app),
    ("contacts", contacts_app),
    ("automatic-replies", automatic_replies_app),
    ("inbox-rules", inbox_rules_app),
    ("contact-groups", contact_groups_app),
    ("directory", directory_app),
    ("recipients", recipients_app),
    ("tasks", tasks_app),
    ("scheduled", scheduled_app),
    ("scheduler", scheduler_app),
    ("headless", headless_app),
    ("startup", startup_app),
    ("recurring", recurring_app),
    ("backup", backup_app),
    ("index", index_app),
    ("ocr", ocr_app),
    ("semantic", semantic_app),
    ("clusters", clusters_app),
    ("insights", insights_app),
    ("cleanup", cleanup_app),
):
    app.add_typer(group, name=name, hidden=name in ADVANCED)
    if name in ADVANCED:
        advanced_app.add_typer(group, name=name)

for command in app.registered_commands:
    # Hidden top-level commands stay callable, and are listed under `advanced`.
    if command.hidden and command.callback is not None:
        advanced_app.command(command.name, help=command.help)(command.callback)
app.add_typer(advanced_app, name="advanced")


__all__ = ["app", "core"]
