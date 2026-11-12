"""Retain footer keys while their rendered bindings and dispatch context are unchanged."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.widgets import Footer
from textual.widgets._footer import FooterKey


class StableFooter(Footer):
    """Keep the framework renderer/click behavior; avoid identical DOM reconstruction."""

    def __init__(self, *children: Any, **kwargs: Any):
        super().__init__(*children, **kwargs)
        self._rendered_bindings: tuple[object, ...] | None = None
        self._recompose_lock = asyncio.Lock()

    def _binding_signature(self) -> tuple[object, ...]:
        palette = self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE
        rows = tuple(
            (
                key, id(namespace), binding.key, self.app.get_key_display(binding),
                binding.description, binding.action, binding.show, repr(binding.group),
                bool(enabled), tooltip,
            )
            for key, (namespace, binding, enabled, tooltip) in self.screen.active_bindings.items()
            if binding.show or (palette and key == self.app.COMMAND_PALETTE_BINDING)
        )
        return self._bindings_ready, self.compact, self.combine_groups, palette, rows

    def _release_closed_key_watchers(self) -> None:
        # Textual 7.5 data_bind holds strong compact-watch references to FooterKeys.
        # Its own pruning runs only when compact changes; ordinary context rebuilds
        # leave that value unchanged. Mirror that pruning for this footer's closed
        # keys only. Keep live keys and every unrelated subscriber intact.
        watchers = getattr(self, "__watchers", {}).get("compact")
        if isinstance(watchers, list):
            watchers[:] = [(node, callback) for node, callback in watchers
                           if not (isinstance(node, FooterKey) and node._closing)]

    async def recompose(self) -> None:
        # The framework may queue two binding updates for one focus transition.
        # Serialize them and compare again at execution time, preserving late changes.
        async with self._recompose_lock:
            if not self.is_attached:
                return
            signature = self._binding_signature()
            if signature == self._rendered_bindings:
                return
            # Interrupted removal/mounting must never leave a partial DOM marked current.
            self._rendered_bindings = None
            try:
                await super().recompose()
                self._rendered_bindings = signature
            finally:
                self._release_closed_key_watchers()
