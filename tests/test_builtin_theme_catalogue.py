"""Precomputed built-ins preserve authoritative recipes and fail safely to generation."""

import gzip
import hashlib
import json
from dataclasses import replace
from importlib.resources import files

import pytest
from qa.build_theme_catalogue import catalogue_bytes

import gucmail.tui.themes as themes


@pytest.fixture(autouse=True)
def fresh_catalogue_cache():
    themes.builtin_themes.cache_clear()
    yield
    themes.builtin_themes.cache_clear()


def test_shipped_catalogue_exactly_matches_all_recipes_fields_and_order():
    expected = themes._generate_builtin_themes()
    actual = themes._load_builtin_catalogue()
    assert len(actual) == 661 and actual == expected
    assert all(not any(issue.severity is themes.ValidationSeverity.ERROR
                       for issue in themes.validate_theme(theme)) for theme in actual)
    assert len({theme.visual_signature for theme in actual}) == 661
    data, _decoded, count = catalogue_bytes()
    assert count == 661
    assert files("gucmail").joinpath("data", "builtin-themes.json.gz").read_bytes() == data
    assert hashlib.sha256(data).hexdigest() == themes._BUILTIN_CATALOGUE_SHA256
    assert len(data) < 150_000


def test_ordinary_catalogue_load_does_not_run_recipe_generation(monkeypatch):
    original = themes._read_builtin_catalogue
    reads = []

    def observed():
        reads.append(True)
        return original()

    monkeypatch.setattr(themes, "_read_builtin_catalogue", observed)
    monkeypatch.setattr(
        themes, "_generate_builtin_themes", lambda: pytest.fail("Regenerated built-ins")
    )
    first, second = themes.ThemeRegistry(), themes.ThemeRegistry()
    assert first.builtins == second.builtins and len(first.builtins) == 661
    assert reads == [True]


@pytest.mark.parametrize("failure", ["missing", "denied", "corrupt", "truncated", "oversized"])
def test_missing_or_corrupt_resource_preserves_complete_original_catalogue(monkeypatch, failure):
    expected = themes._generate_builtin_themes()
    data = themes._read_builtin_catalogue()

    def damaged():
        if failure == "missing":
            raise FileNotFoundError("Synthetic missing optional asset")
        if failure == "denied":
            raise PermissionError("Synthetic asset access rejection")
        return {"corrupt": b"invalid gzip", "truncated": data[:100],
                "oversized": b"x" * (themes._BUILTIN_CATALOGUE_MAX_BYTES + 1)}[failure]

    monkeypatch.setattr(themes, "_read_builtin_catalogue", damaged)
    assert themes.builtin_themes() == expected


@pytest.mark.parametrize("failure", ["json", "schema", "count", "generator", "palette", "bomb"])
def test_pinned_but_invalid_decoded_asset_is_bounded_and_falls_back(monkeypatch, failure):
    expected = themes._generate_builtin_themes()
    raw = json.loads(gzip.decompress(themes._read_builtin_catalogue()))
    if failure == "schema":
        raw["schema"] = "future"
    elif failure == "count":
        raw["themes"].pop()
    elif failure == "generator":
        raw["generator"] = "future"
    elif failure == "palette":
        raw["themes"][0]["palette"]["app_background"] = "terminal-hostile-invalid"
    decoded = (b"not json" if failure == "json" else
               b"x" * (themes._BUILTIN_CATALOGUE_MAX_JSON + 100) if failure == "bomb" else
               json.dumps(raw).encode())
    data = gzip.compress(decoded, mtime=0)
    monkeypatch.setattr(themes, "_read_builtin_catalogue", lambda: data)
    monkeypatch.setattr(themes, "_BUILTIN_CATALOGUE_SHA256", hashlib.sha256(data).hexdigest())
    assert themes.builtin_themes() == expected


def test_registry_customization_does_not_mutate_shared_builtin_definitions():
    first, second = themes.ThemeRegistry(), themes.ThemeRegistry()
    original = second.get("builtin.default-dark")
    custom = first.duplicate("builtin.default-dark", name="Synthetic local variant",
                             custom_id="custom.synthetic-catalogue")
    edited = replace(custom, name="Synthetic renamed variant")
    first.add_custom(edited)
    assert second.custom == () and first.get(edited.id).name == edited.name
    assert first.get(original.id) == second.get(original.id) == original
