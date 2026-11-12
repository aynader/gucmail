"""An explicit mailbox search route must outrank the configured default."""

from pathlib import Path

import pytest

from gucmail.commands import CommandInvocation
from gucmail.models import SearchQuery
from gucmail.settings import SettingsService
from gucmail.tui.app import GUCmailApp
from gucmail.tui.palette_routes import catalog_route
from test_ui_session import context_for


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "default_scope", ["current_folder", "mailbox", "local_index", "backups", "all"]
)
@pytest.mark.parametrize("route_name", ["search.search_current_folder", "search.search_all_mail"])
async def test_explicit_mail_scope_outranks_each_configured_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, default_scope: str, route_name: str
) -> None:
    context = context_for(tmp_path)
    SettingsService(context.config_store).set("search.default_scope", default_scope)
    app = GUCmailApp(context, offline=True)
    searches: list[SearchQuery] = []
    local: list[dict[str, object]] = []
    monkeypatch.setattr(app, "_search_mail", lambda query, _label: searches.append(query))
    monkeypatch.setattr(app, "_open_local_search", lambda **kwargs: local.append(kwargs))
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            route = catalog_route(route_name)
            app._catalog_search(
                route,
                CommandInvocation.from_mapping(route.command_id, {"query": "synthetic scope term"}),
            )
            assert len(searches) == 1
            assert not local
            assert searches[0].text == "synthetic scope term"
            assert searches[0].folder == (
                app.ui.folder if route_name.endswith("current_folder") else None
            )
            assert app.gateway is None
            assert (
                SettingsService(context.config_store).value("search.default_scope").effective_value
                == default_scope
            )
    finally:
        context.close()
