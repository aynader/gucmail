"""Crash-safe persistence for user-created themes.

The regular application configuration only stores the selected theme ID and
appearance preferences.  Custom theme definitions live in this deliberately
separate, non-secret repository.  The format is bounded, versioned, human
readable, and parsed exclusively as JSON.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Final, TypeVar

from gucmail.tui.themes import (
    ThemeDefinition,
    ThemeRegistry,
    ThemeValidationError,
    export_theme,
    import_theme,
)

REPOSITORY_SCHEMA_VERSION: Final = 1
REPOSITORY_FORMAT: Final = "gucmail-custom-themes"
MAX_REPOSITORY_BYTES: Final = 2 * 1024 * 1024
MAX_CUSTOM_THEMES: Final = 256
LOCK_TIMEOUT_SECONDS: Final = 2.0
LOCK_POLL_SECONDS: Final = 0.025


class ThemeRepositoryError(ThemeValidationError):
    """Raised when a custom-theme repository operation cannot complete safely."""


class ThemeRepositoryBusyError(ThemeRepositoryError):
    """Raised instead of silently overwriting another process's update."""


class ThemeRepositoryConflictError(ThemeRepositoryError):
    """Raised when an optimistic revision no longer matches the file on disk."""


@dataclass(frozen=True, slots=True)
class ThemeRepositorySnapshot:
    """One validated repository state and any recovery action taken to load it."""

    themes: tuple[ThemeDefinition, ...]
    revision: int = 0
    recovered_from: Path | None = None
    quarantined: tuple[Path, ...] = ()

    @property
    def recovered(self) -> bool:
        return self.recovered_from is not None


_ResultT = TypeVar("_ResultT")


class CustomThemeRepository:
    """Persist custom themes with atomic replacement, backup, and quarantine.

    A sibling ``.backup`` file stores the last known-good revision.  Malformed
    primary data is moved aside rather than overwritten; a valid backup is then
    restored atomically.  If neither copy is usable, loading returns an empty
    repository while retaining every malformed byte in quarantine for manual
    inspection.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = MAX_REPOSITORY_BYTES,
        max_themes: int = MAX_CUSTOM_THEMES,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if max_bytes < 1024:
            raise ValueError("Theme repository limit must be at least 1024 bytes")
        if not 1 <= max_themes <= 10_000:
            raise ValueError("Custom-theme limit must be between 1 and 10000")
        if lock_timeout <= 0:
            raise ValueError("Theme repository lock timeout must be positive")
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.backup")
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.max_bytes = max_bytes
        self.max_themes = max_themes
        self.lock_timeout = lock_timeout

    def load(self) -> ThemeRepositorySnapshot:
        """Load validated themes, recovering or quarantining damaged JSON."""

        return self._load_unlocked()

    def save(
        self,
        themes: Iterable[ThemeDefinition],
        *,
        expected_revision: int | None = None,
    ) -> ThemeRepositorySnapshot:
        """Atomically replace the repository after validating every theme."""

        validated = self._validated_themes(themes)
        with self._lock():
            current = self._load_unlocked()
            self._check_revision(current, expected_revision)
            return self._save_unlocked(validated, revision=current.revision + 1)

    def add(
        self,
        theme: ThemeDefinition,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        def add_theme(registry: ThemeRegistry) -> ThemeDefinition:
            if theme.id in {item.id for item in registry.custom}:
                raise ThemeRepositoryError(f"Custom theme already exists: {theme.id}")
            return registry.add_custom(theme)

        return self._mutate(add_theme, expected_revision=expected_revision)

    def duplicate(
        self,
        source: ThemeDefinition,
        *,
        name: str,
        custom_id: str | None = None,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        """Duplicate a supplied built-in or custom definition into the repository."""

        def duplicate(registry: ThemeRegistry) -> ThemeDefinition:
            if source.id.startswith("custom.") and source.id not in {
                theme.id for theme in registry.custom
            }:
                registry.add_custom(source)
            elif source.id.startswith("builtin."):
                # The canonical built-in registry already contains the source.
                canonical = registry.get(source.id)
                if canonical.visual_signature != source.visual_signature:
                    raise ThemeRepositoryError("Built-in source does not match the catalog")
            return registry.duplicate(source.id, name=name, custom_id=custom_id)

        return self._mutate(duplicate, expected_revision=expected_revision)

    def edit(
        self,
        theme_id: str,
        *,
        palette_overrides: Mapping[str, str | Sequence[str]] | None = None,
        style_overrides: Mapping[str, object] | None = None,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        return self._mutate(
            lambda registry: registry.edit_custom(
                theme_id,
                palette_overrides=palette_overrides,
                style_overrides=style_overrides,
            ),
            expected_revision=expected_revision,
        )

    def rename(
        self,
        theme_id: str,
        name: str,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        return self._mutate(
            lambda registry: registry.rename_custom(theme_id, name),
            expected_revision=expected_revision,
        )

    def replace_from_payload(
        self,
        payload: str,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        """Replace an existing custom theme with a fully validated JSON definition."""

        replacement = self._import_definition(payload)

        def replace_theme(registry: ThemeRegistry) -> ThemeDefinition:
            registry.delete_custom(replacement.id)
            return registry.add_custom(replacement)

        return self._mutate(replace_theme, expected_revision=expected_revision)

    def delete(
        self,
        theme_id: str,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        return self._mutate(
            lambda registry: registry.delete_custom(theme_id),
            expected_revision=expected_revision,
        )

    def reset(self, *, expected_revision: int | None = None) -> ThemeRepositorySnapshot:
        with self._lock():
            current = self._load_unlocked()
            self._check_revision(current, expected_revision)
            return self._save_unlocked((), revision=current.revision + 1)

    def import_payload(
        self,
        payload: str,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        theme = self._import_definition(payload)
        return self.add(theme, expected_revision=expected_revision)

    def import_file(
        self,
        path: Path,
        *,
        expected_revision: int | None = None,
    ) -> ThemeDefinition:
        raw = self._bounded_read(Path(path), limit=128 * 1024)
        try:
            payload = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ThemeRepositoryError("Theme import must be UTF-8 JSON") from exc
        return self.import_payload(payload, expected_revision=expected_revision)

    def export(self, theme_id: str) -> str:
        registry = ThemeRegistry(self.load().themes)
        return export_theme(registry.get(theme_id))

    def export_file(self, theme_id: str, path: Path, *, overwrite: bool = False) -> Path:
        """Write one human-readable custom theme without following a broad glob."""

        target = Path(path).expanduser()
        if target.exists() and not overwrite:
            raise ThemeRepositoryError(f"Theme export already exists: {target}")
        payload = self.export(theme_id).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, payload)
        return target

    def recover_from_backup(self) -> ThemeRepositorySnapshot:
        """Explicitly restore the last known-good backup revision."""

        with self._lock():
            if not self.backup_path.exists():
                raise ThemeRepositoryError("No custom-theme backup is available")
            raw = self._bounded_read(self.backup_path)
            revision, themes = self._decode(raw)
            quarantined: tuple[Path, ...] = (
                (self._quarantine(self.path),) if self.path.exists() else ()
            )
            self._atomic_write(self.path, raw)
            return ThemeRepositorySnapshot(
                themes,
                revision,
                recovered_from=self.backup_path,
                quarantined=quarantined,
            )

    def _mutate(
        self,
        operation: Callable[[ThemeRegistry], _ResultT],
        *,
        expected_revision: int | None,
    ) -> _ResultT:
        with self._lock():
            current = self._load_unlocked()
            self._check_revision(current, expected_revision)
            registry = ThemeRegistry(current.themes)
            result = operation(registry)
            self._save_unlocked(registry.custom, revision=current.revision + 1)
            return result

    @staticmethod
    def _import_definition(payload: str) -> ThemeDefinition:
        try:
            return import_theme(payload)
        except ThemeValidationError as exc:
            raise ThemeRepositoryError(str(exc)) from exc

    def _load_unlocked(self) -> ThemeRepositorySnapshot:
        quarantined: list[Path] = []
        if self.path.exists():
            try:
                revision, themes = self._decode(self._bounded_read(self.path))
                return ThemeRepositorySnapshot(themes, revision)
            except (OSError, ThemeRepositoryError, ThemeValidationError):
                quarantined.append(self._quarantine(self.path))
        if self.backup_path.exists():
            try:
                raw = self._bounded_read(self.backup_path)
                revision, themes = self._decode(raw)
            except (OSError, ThemeRepositoryError, ThemeValidationError):
                quarantined.append(self._quarantine(self.backup_path))
            else:
                self._atomic_write(self.path, raw)
                return ThemeRepositorySnapshot(
                    themes,
                    revision,
                    recovered_from=self.backup_path,
                    quarantined=tuple(quarantined),
                )
        return ThemeRepositorySnapshot((), quarantined=tuple(quarantined))

    def _save_unlocked(
        self,
        themes: Iterable[ThemeDefinition],
        *,
        revision: int,
    ) -> ThemeRepositorySnapshot:
        validated = self._validated_themes(themes)
        encoded = self._encode(validated, revision=revision)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                current = self._bounded_read(self.path)
                self._decode(current)
            except (OSError, ThemeRepositoryError, ThemeValidationError):
                self._quarantine(self.path)
            else:
                self._atomic_write(self.backup_path, current)
        self._atomic_write(self.path, encoded)
        return ThemeRepositorySnapshot(validated, revision)

    def _validated_themes(self, themes: Iterable[ThemeDefinition]) -> tuple[ThemeDefinition, ...]:
        values = tuple(themes)
        if len(values) > self.max_themes:
            raise ThemeRepositoryError(
                f"Custom-theme repository is limited to {self.max_themes} themes"
            )
        registry = ThemeRegistry(values)
        if len(registry.custom) != len(values):
            raise ThemeRepositoryError("Custom-theme IDs must be unique")
        return tuple(sorted(registry.custom, key=lambda theme: theme.id))

    def _encode(self, themes: tuple[ThemeDefinition, ...], *, revision: int) -> bytes:
        if revision < 0:
            raise ThemeRepositoryError("Theme repository revision cannot be negative")
        payload = {
            "format": REPOSITORY_FORMAT,
            "schema_version": REPOSITORY_SCHEMA_VERSION,
            "revision": revision,
            "themes": [json.loads(export_theme(theme)) for theme in themes],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > self.max_bytes:
            raise ThemeRepositoryError(f"Custom-theme repository exceeds {self.max_bytes} bytes")
        return encoded

    def _decode(self, raw: bytes) -> tuple[int, tuple[ThemeDefinition, ...]]:
        if len(raw) > self.max_bytes:
            raise ThemeRepositoryError(f"Custom-theme repository exceeds {self.max_bytes} bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ThemeRepositoryError("Custom-theme repository is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ThemeRepositoryError("Custom-theme repository must contain one object")
        if set(payload) != {"format", "schema_version", "revision", "themes"}:
            raise ThemeRepositoryError("Custom-theme repository fields are incomplete or unknown")
        if payload.get("format") != REPOSITORY_FORMAT:
            raise ThemeRepositoryError("Unrecognized custom-theme repository format")
        if payload.get("schema_version") != REPOSITORY_SCHEMA_VERSION:
            raise ThemeRepositoryError("Unsupported custom-theme repository schema")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ThemeRepositoryError("Invalid custom-theme repository revision")
        raw_themes = payload.get("themes")
        if not isinstance(raw_themes, list) or len(raw_themes) > self.max_themes:
            raise ThemeRepositoryError("Custom-theme list is invalid or exceeds its bound")
        themes: list[ThemeDefinition] = []
        for raw_theme in raw_themes:
            try:
                serialized = json.dumps(raw_theme, ensure_ascii=True)
            except (TypeError, ValueError) as exc:
                raise ThemeRepositoryError("Custom-theme entry is not JSON serializable") from exc
            themes.append(import_theme(serialized))
        return revision, self._validated_themes(themes)

    def _bounded_read(self, path: Path, *, limit: int | None = None) -> bytes:
        maximum = limit or self.max_bytes
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ThemeRepositoryError(f"Unable to inspect custom-theme file: {path}") from exc
        if size > maximum:
            raise ThemeRepositoryError(f"Custom-theme file exceeds {maximum} bytes")
        try:
            with path.open("rb") as handle:
                data = handle.read(maximum + 1)
        except OSError as exc:
            raise ThemeRepositoryError(f"Unable to read custom-theme file: {path}") from exc
        if len(data) > maximum:
            raise ThemeRepositoryError(f"Custom-theme file exceeds {maximum} bytes")
        return data

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            raise ThemeRepositoryError(
                f"Unable to atomically write custom themes: {target}"
            ) from exc
        finally:
            if temporary is not None and temporary.exists():
                with suppress(OSError):
                    temporary.unlink()

    def _quarantine(self, source: Path) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = source.with_name(
            f"{source.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}{source.suffix}"
        )
        try:
            os.replace(source, destination)
        except OSError as exc:
            raise ThemeRepositoryError(
                f"Unable to quarantine damaged theme file: {source}"
            ) from exc
        return destination

    @staticmethod
    def _check_revision(
        current: ThemeRepositorySnapshot,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is not None and current.revision != expected_revision:
            raise ThemeRepositoryConflictError(
                "Custom themes changed in another process; reload before applying this edit"
            )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout
        token = f"{os.getpid()}:{uuid.uuid4().hex}".encode("ascii")
        while True:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ThemeRepositoryBusyError(
                        "Custom themes are being updated by another process; retry shortly"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
                continue
            except OSError as exc:
                raise ThemeRepositoryError("Unable to lock the custom-theme repository") from exc
            try:
                os.write(descriptor, token)
            finally:
                os.close(descriptor)
            break
        try:
            yield
        finally:
            try:
                if self.lock_path.read_bytes() == token:
                    self.lock_path.unlink()
            except OSError:
                pass


__all__ = [
    "MAX_CUSTOM_THEMES",
    "MAX_REPOSITORY_BYTES",
    "REPOSITORY_FORMAT",
    "REPOSITORY_SCHEMA_VERSION",
    "CustomThemeRepository",
    "ThemeRepositoryBusyError",
    "ThemeRepositoryConflictError",
    "ThemeRepositoryError",
    "ThemeRepositorySnapshot",
]
