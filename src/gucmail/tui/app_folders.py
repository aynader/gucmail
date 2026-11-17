"""Main window: the folder tree and folder actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import work
from textual.widgets import (
    Static,
    Tree,
)

from gucmail.errors import (
    ConfigurationError,
)
from gucmail.models import (
    FolderInfo,
)
from gucmail.security import sanitize_terminal
from gucmail.tui.app_core import AppCore
from gucmail.tui.app_helpers import _is_mail_folder_class, _safe_failure
from gucmail.tui.write_coordinator import (
    coordinated_write,
)

if TYPE_CHECKING:
    from gucmail.commands import CommandContext, CommandInvocation
    from gucmail.services._base import MutationPlan


class FoldersMixin(AppCore):
    """Methods of GUCmailApp; see gucmail.tui.app."""

    def _persist_folder_expansion(self) -> None:
        self._persist_setting_values(
            {"mail.folder_expansion": sorted(self._expanded_folders)},
            reason="Folder expansion updated",
        )

    @staticmethod
    def _folder_label(folder: FolderInfo) -> str:
        # Like Outlook, show only the unread count, and only when there is something unread.
        name = sanitize_terminal(folder.name, max_length=180) or "(unnamed folder)"
        if folder.unread_count:
            return f"{name}  {folder.unread_count}"
        return name

    def _request_folder_children(
        self,
        *,
        parent_reference: str | None,
        node_id: Any | None,
        offset: int = 0,
        append: bool = False,
    ) -> None:
        if node_id is None or self.offline:
            return
        self._folder_load_sequence += 1
        token = self._folder_load_sequence
        self._folder_load_tokens[node_id] = token
        try:
            node = self.query_one("#folder-tree", Tree).get_node_by_id(node_id)
        except Exception:
            return
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": True, "loaded": False})
        node.data = data
        self._load_folder_children(
            token,
            node_id,
            parent_reference=parent_reference,
            offset=offset,
            append=append,
        )

    @work(thread=True, exclusive=False, group="folder-load")
    def _load_folder_children(
        self,
        token: int,
        node_id: Any,
        *,
        parent_reference: str | None,
        offset: int,
        append: bool,
    ) -> None:
        from gucmail.services.folders import FolderService

        try:
            gateway = self._ensure_gateway()
            service = FolderService(gateway)
            page = service.list_folders(
                parent_reference,
                limit=min(max(self.context.config.page_size, 25), 100),
                offset=offset,
            )
            self.call_from_thread(
                self._render_folder_children,
                token,
                node_id,
                parent_reference,
                page,
                append,
            )
        except Exception as exc:
            self.call_from_thread(
                self._render_folder_error,
                token,
                node_id,
                _safe_failure(exc, action="Couldn't load folders"),
            )

    def _render_folder_children(
        self,
        token: int,
        node_id: Any,
        parent_reference: str | None,
        page: Any,
        append: bool,
    ) -> None:
        if self._folder_load_tokens.get(node_id) != token:
            return
        tree = self.query_one("#folder-tree", Tree)
        try:
            node = tree.get_node_by_id(node_id)
        except Exception:
            return
        if not append:
            self._forget_folder_nodes(node)
            node.remove_children()
        else:
            for child in tuple(node.children):
                child_data = child.data
                if isinstance(child_data, dict) and child_data.get("kind") == "folder_more":
                    child.remove()
        for folder in page.items:
            if not _is_mail_folder_class(folder.folder_class):
                # Calendar/Contacts/Tasks have their own workspaces; Exchange system
                # folders (Recipient Cache, Conversation Action Settings, Files,
                # Journal, Notes) only clutter the mail tree. Unknown classes stay.
                continue
            reference = str(folder.reference)
            child_data = {
                "kind": "folder_branch" if folder.child_count != 0 else "folder",
                "reference": reference,
                "loaded": folder.child_count == 0,
                "path": folder.path,
                "folder": folder,
            }
            if folder.child_count == 0:
                child = node.add_leaf(Text(self._folder_label(folder)), data=child_data)
            else:
                child = node.add(Text(self._folder_label(folder)), data=child_data)
                child.add_leaf("Loading…", data={"kind": "placeholder"})
            self._folder_nodes[reference] = child.id
            self._folder_paths[reference] = folder.path
            auto_expand = self._folder_expand_all_remaining > 0 and folder.child_count != 0
            if auto_expand:
                self._folder_expand_all_remaining -= 1
            if (reference in self._expanded_folders or auto_expand) and folder.child_count != 0:
                child.expand()
            if reference == self.ui.folder:
                # Reflect the current location without queuing NodeSelected,
                # whose later delivery could undo a newer palette navigation.
                self.call_after_refresh(
                    self._sync_folder_cursor, tree, child, reference, tree.cursor_node
                )
                if self._folder_selection_to_restore == reference:
                    # Removing a refreshed subtree makes Textual temporarily move
                    # the cursor to an ancestor. Re-select after the replacement
                    # nodes finish, unless a newer user selection cancels the token.
                    self.call_after_refresh(
                        self._restore_folder_selection,
                        tree,
                        child,
                        reference,
                    )
        auto_page_offset: int | None = None
        if page.has_more and page.next_offset is not None:
            if self._folder_expand_all_remaining > 0:
                self._folder_expand_all_remaining -= 1
                auto_page_offset = page.next_offset
            else:
                node.add_leaf(
                    "Load more folders…",
                    data={
                        "kind": "folder_more",
                        "reference": parent_reference,
                        "offset": page.next_offset,
                    },
                )
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": False, "loaded": True})
        node.data = data
        if parent_reference is not None:
            self._folder_refresh_pending_expanded.discard(parent_reference)
        if self._folder_refresh_in_progress and not self._folder_refresh_pending_expanded:
            # Removing a branch's loading sentinel may enqueue NodeCollapsed.
            # Let those programmatic events drain before user collapses become
            # authoritative again.
            generation = self._folder_refresh_generation
            self.set_timer(
                0.25,
                lambda: self._finish_folder_refresh(generation),
            )
        if auto_page_offset is not None:
            self._request_folder_children(
                parent_reference=parent_reference,
                node_id=node.id,
                offset=auto_page_offset,
                append=True,
            )

    def _restore_folder_selection(self, tree: Tree[Any], node: Any, reference: str) -> None:
        """Restore a refresh selection unless the user selected something newer."""

        if self._folder_selection_to_restore == reference and self.ui.folder == reference:
            self._folder_selection_to_restore = None
            if self._folder_refresh_expanded_snapshot:
                self._expanded_folders.update(self._folder_refresh_expanded_snapshot)
                self._persist_folder_expansion()
            tree.move_cursor(node)

    def _sync_folder_cursor(
        self, tree: Tree[Any], node: Any, reference: str, previous_cursor: Any
    ) -> None:
        """Wait for new node lines without replacing a newer user cursor."""

        if (
            tree.is_mounted
            and self.ui.folder == reference
            and self._folder_nodes.get(reference) == node.id
            and tree.cursor_node is previous_cursor
        ):
            tree.move_cursor(node)

    def _load_rendered_folder_pages_for_expand_all(self, root: Any) -> None:
        """Consume existing pagination leaves while the bounded expand-all budget permits."""

        pending = [root]
        while pending and self._folder_expand_all_remaining > 0:
            parent = pending.pop()
            for child in tuple(parent.children):
                data = child.data
                if isinstance(data, dict) and data.get("kind") == "folder_more":
                    offset = data.get("offset")
                    if not isinstance(offset, int):
                        continue
                    reference = data.get("reference")
                    self._folder_expand_all_remaining -= 1
                    child.remove()
                    self._request_folder_children(
                        parent_reference=str(reference) if reference is not None else None,
                        node_id=parent.id,
                        offset=offset,
                        append=True,
                    )
                else:
                    pending.append(child)

    def _finish_folder_refresh(self, generation: int | None = None) -> None:
        """Release refresh event suppression after saved open branches are restored."""

        if generation is not None and generation != self._folder_refresh_generation:
            return
        if not self._folder_refresh_in_progress:
            return
        self._expanded_folders.update(self._folder_refresh_expanded_snapshot)
        self._persist_folder_expansion()
        self._folder_refresh_in_progress = False
        self._folder_refresh_preserve_expanded.clear()
        self._folder_refresh_expanded_snapshot.clear()
        self._folder_refresh_pending_expanded.clear()

    def _forget_folder_nodes(self, node: Any) -> None:
        """Remove stale identity/path entries before replacing one loaded subtree."""

        pending = list(node.children)
        while pending:
            child = pending.pop()
            pending.extend(child.children)
            data = child.data
            if not isinstance(data, dict):
                continue
            reference = data.get("reference")
            if isinstance(reference, str):
                if reference in self._expanded_folders:
                    self._folder_refresh_preserve_expanded.add(reference)
                self._folder_nodes.pop(reference, None)
                self._folder_paths.pop(reference, None)

    def _render_folder_error(self, token: int, node_id: Any, message: str) -> None:
        if self._folder_load_tokens.get(node_id) != token:
            return
        try:
            node = self.query_one("#folder-tree", Tree).get_node_by_id(node_id)
        except Exception:
            return
        node.remove_children()
        node.add_leaf("Folder loading failed · press F5 to retry", data={"kind": "placeholder"})
        data = dict(node.data) if isinstance(node.data, dict) else {}
        data.update({"loading": False, "loaded": False})
        node.data = data
        self._set_status(message)

    def _palette_open_folder(self, invocation: CommandInvocation, _context: CommandContext) -> None:
        presets = {
            "mail.open_inbox": "inbox",
            "mail.open_sent": "sent",
            "mail.open_drafts": "drafts",
            "mail.open_deleted": "trash",
            "mail.open_junk": "junk",
            "mail.open_archive": "archive",
        }
        folder = presets.get(invocation.command_id) or self._palette_scalar(
            invocation.get("folder")
        )
        if not folder:
            self._set_status("Choose a folder in the palette before opening it.")
            return
        self._activate_folder_location(folder)

    def _activate_folder_location(self, folder: str, *, record_history: bool = True) -> None:
        normalized = folder.strip()
        if not normalized:
            return
        if normalized != self.ui.folder:
            self._cancel_automatic_read()
            self._message_generation += 1
            if self._message_timeout is not None:
                self._message_timeout.stop()
                self._message_timeout = None
            self._message_detail = None
            self._message_detail_reference = None
            self.ui.selected_reference = None
            self._selected_message_references.clear()
            self.query_one("#preview", Static).update("Select a message to preview it safely.")
        if normalized != self.ui.folder and record_history:
            if (
                not self._folder_location_history
                or self._folder_location_history[-1] != self.ui.folder
            ):
                self._folder_location_history.append(self.ui.folder)
            if self._folder_location_history[-1] != normalized:
                self._folder_location_history.append(normalized)
            del self._folder_location_history[:-50]
        if record_history:
            self._recent_folder_paths = [
                normalized,
                *(path for path in self._recent_folder_paths if path != normalized),
            ][:20]
        self.ui.folder = normalized
        self._persist_setting_values(
            {
                "mail.last_folder": normalized,
                "palette.recent_folders": self._recent_folder_paths,
            },
            reason="Folder history updated",
        )
        self.refresh_mailbox()

    def _selected_exchange_folder(self) -> tuple[str, str] | None:
        tree = self.query_one("#folder-tree", Tree)
        node = tree.cursor_node
        data = node.data if node is not None else None
        if not isinstance(data, dict) or data.get("kind") not in {"folder", "folder_branch"}:
            return None
        reference = str(data.get("reference") or "")
        if not reference or reference not in self._folder_nodes:
            return None
        path = str(data.get("path") or self._folder_paths.get(reference) or reference)
        return reference, path

    def _selected_folder_info(self) -> FolderInfo | None:
        tree = self.query_one("#folder-tree", Tree)
        node = tree.cursor_node
        data = node.data if node is not None else None
        if not isinstance(data, dict):
            return None
        folder = data.get("folder")
        return folder if isinstance(folder, FolderInfo) else None

    def _prompt_folder_create(self, invocation: CommandInvocation) -> None:
        from gucmail.tui.dialogs import FolderNameScreen

        parent = self._selected_folder_info()
        parent_reference = parent.reference if parent is not None else None
        parent_label = parent.path if parent is not None else "the mailbox root"
        initial_name = self._palette_scalar(invocation.get("folder")) or ""

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_mutation(
                    "create",
                    reference=parent_reference,
                    value=value,
                )

        self.push_screen(
            FolderNameScreen(
                "Create folder",
                f"Parent: {parent_label}\nEnter the new folder display name.",
                initial_name=initial_name,
                submit_label="Preview create",
            ),
            submitted,
        )

    def _palette_create_folder(
        self,
        invocation: CommandInvocation,
        _context: CommandContext,
    ) -> None:
        self._prompt_folder_create(invocation)

    def _prompt_folder_rename(self) -> None:
        from gucmail.tui.dialogs import FolderNameScreen

        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Rename folder.")
            return

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_mutation(
                    "rename",
                    reference=folder.reference,
                    value=value,
                )

        self.push_screen(
            FolderNameScreen(
                "Rename folder",
                f"Folder: {folder.path}\nEnter the replacement display name.",
                initial_name=folder.name,
                submit_label="Preview rename",
            ),
            submitted,
        )

    def _prompt_folder_delete(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Delete folder.")
            return
        self._prepare_folder_mutation("delete", reference=folder.reference)

    def _prompt_folder_empty(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before choosing Empty folder.")
            return
        self._prepare_folder_mutation("empty", reference=folder.reference)

    @work(thread=True, exclusive=True, group="folder-mutation")
    def _prepare_folder_empty_path(self, folder: str) -> None:
        """Resolve an explicit safe folder selector before showing strong confirmation."""

        from gucmail.services.folders import FolderService

        try:
            plan = FolderService(self._ensure_gateway()).plan_empty(folder)
            self.call_from_thread(self._confirm_folder_mutation, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't empty the folder"),
            )

    @work(thread=True, exclusive=True, group="folder-mutation")
    def _prepare_folder_mutation(
        self,
        operation: str,
        *,
        reference: str | None,
        value: str | None = None,
    ) -> None:
        from gucmail.services.folders import FolderService

        try:
            service = FolderService(self._ensure_gateway())
            if operation == "create":
                plan = service.plan_create(
                    value or "",
                    parent=f"id:{reference}" if reference is not None else None,
                )
            elif operation == "rename":
                plan = service.plan_rename(f"id:{reference}", value or "")
            elif operation == "delete":
                plan = service.plan_delete(f"id:{reference}")
            elif operation == "empty":
                plan = service.plan_empty(f"id:{reference}")
            else:
                raise ConfigurationError(f"Unsupported folder operation: {operation}")
            self.call_from_thread(self._confirm_folder_mutation, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action=f"Couldn't {operation} the folder"),
            )

    def _confirm_folder_mutation(self, plan: MutationPlan) -> None:
        from gucmail.tui.dialogs import ConfirmationScreen

        operation = plan.operation.removeprefix("folder_").replace("_", " ")
        detail = plan.summary
        if plan.operation == "folder_empty":
            detail += (
                "\n\nStrong confirmation: this affects every item in the selected folder. "
                "The folder itself remains."
            )
        elif plan.operation == "folder_delete":
            detail += "\n\nThe selected folder and its contents will move to Deleted Items."

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_folder_mutation(plan)

        self.push_screen(
            ConfirmationScreen(f"Confirm folder {operation}", detail),
            confirmed,
        )

    @work(thread=True, exclusive=False, group="folder-mutation")
    @coordinated_write()
    def _execute_folder_mutation(self, plan: MutationPlan) -> None:
        from gucmail.services.folders import FolderService

        try:
            execution = FolderService(self._ensure_gateway()).execute_mutation(
                plan,
                confirmation_scope=(
                    plan.confirmation_scope if plan.requires_confirmation else None
                ),
            )
            self.call_from_thread(self._folder_mutation_finished, plan, execution.results)
        except Exception as exc:
            operation = plan.operation.removeprefix("folder_").replace("_", " ")
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action=f"Couldn't {operation} the folder"),
            )

    def _folder_mutation_finished(
        self,
        plan: MutationPlan,
        results: tuple[Any, ...],
    ) -> None:
        operation = plan.operation.removeprefix("folder_").replace("_", " ")
        result = results[0] if results else None
        if isinstance(result, FolderInfo):
            self.ui.folder = result.reference
            self._persist_setting_values(
                {"mail.last_folder": result.reference}, reason="Last folder updated"
            )
            if result.parent_reference:
                self._expanded_folders.add(result.parent_reference)
                self._persist_folder_expansion()
        elif plan.operation == "folder_delete" and self.ui.folder in plan.targets:
            self.ui.folder = self.context.config.default_folder
            self._persist_setting_values(
                {"mail.last_folder": self.ui.folder}, reason="Last folder updated"
            )
        self.action_refresh()
        self._set_status(f"Folder {operation} completed · refreshing tree")

    def _show_selected_folder_details(self) -> None:
        from gucmail.tui.dialogs import FolderDetailsScreen

        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before showing folder details.")
            return
        self.push_screen(FolderDetailsScreen(folder))

    def _show_selected_folder_unread(self) -> None:
        folder = self._selected_folder_info()
        if folder is None:
            self._set_status("Select an Exchange folder before showing its unread count.")
            return
        count = "unavailable" if folder.unread_count is None else str(folder.unread_count)
        total = "unavailable" if folder.total_count is None else str(folder.total_count)
        self._set_status(
            f"{sanitize_terminal(folder.path, max_length=160)} · Unread {count} · Total {total}"
        )

    def _prompt_folder_move(self, invocation: CommandInvocation) -> None:
        from gucmail.tui.dialogs import FolderMoveScreen

        selected = self._selected_exchange_folder()
        if selected is None:
            self._set_status("Select a movable Exchange folder before choosing Move folder.")
            return
        source_reference, source_path = selected
        destination = self._palette_scalar(invocation.get("folder"))
        if destination and destination not in {source_reference, source_path}:
            self._prepare_folder_move(source_reference, destination)
            return

        def submitted(value: str | None) -> None:
            if value:
                self._prepare_folder_move(source_reference, value)

        self.push_screen(FolderMoveScreen(source_path), submitted)

    @work(thread=True, exclusive=True, group="folder-move")
    def _prepare_folder_move(self, source_reference: str, destination: str) -> None:
        from gucmail.services.folders import FolderService

        try:
            plan = FolderService(self._ensure_gateway()).plan_move(
                f"id:{source_reference}",
                destination,
            )
            self.call_from_thread(self._confirm_folder_move, plan)
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't move the folder"),
            )

    def _confirm_folder_move(self, plan: MutationPlan) -> None:
        from gucmail.tui.dialogs import ConfirmationScreen

        def confirmed(value: bool | None) -> None:
            if value:
                self._execute_folder_move(plan)

        self.push_screen(
            ConfirmationScreen("Confirm folder move", plan.summary),
            confirmed,
        )

    @work(thread=True, exclusive=False, group="folder-move")
    @coordinated_write()
    def _execute_folder_move(self, plan: MutationPlan) -> None:
        from gucmail.services.folders import FolderService

        try:
            result = FolderService(self._ensure_gateway()).execute_mutation(
                plan,
                confirmation_scope=plan.confirmation_scope,
            )
            moved = result.results[0]
            destination = moved.path if isinstance(moved, FolderInfo) else "the destination"
            self.call_from_thread(self.action_refresh)
            self.call_from_thread(
                self._set_status,
                "Folder moved to "
                f"{sanitize_terminal(destination, max_length=160)} · refreshing tree",
            )
        except Exception as exc:
            self.call_from_thread(
                self._set_status,
                _safe_failure(exc, action="Couldn't move the folder"),
            )

    def _navigate_visible_mail_folder(self, tree: Tree[Any], *, forward: bool) -> None:
        """Navigate actual visible folders, skipping product areas and paging rows."""
        folders: list[Any] = []

        def visit(node: Any) -> None:
            data = node.data
            if (
                isinstance(data, dict)
                and data.get("kind") in {"folder", "folder_branch"}
                and data.get("reference")
            ):
                folders.append(node)
            if node.is_expanded:
                for child in node.children:
                    visit(child)

        visit(tree.root)
        if not folders:
            self._set_status("Expand the folder tree before navigating adjacent folders.")
            return
        current = next(
            (
                index
                for index, node in enumerate(folders)
                if node.data.get("reference") == self.ui.folder
            ),
            -1 if forward else 0,
        )
        tree.select_node(folders[(current + (1 if forward else -1)) % len(folders)])
