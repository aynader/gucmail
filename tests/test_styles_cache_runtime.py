"""Background-color memoization must not own closed widgets' render caches."""

import gc
import weakref

from textual._styles_cache import StylesCache
from textual.color import Color

from gucmail.tui import runtime


def test_background_colors_match_the_original_calculation_across_alpha_values():
    from textual.style import Style

    colors = (
        Color.parse("#101010"),
        Color.parse("#eeeeee"),
        Color(20, 160, 70, 0.5),
        Color(250, 50, 90, 0.25),
        Color(0, 0, 0, 0),
    )
    for base in colors:
        for background in colors:
            assert StylesCache().get_inner_outer(base, background) == (
                Style(background=base + background),
                Style(background=base),
            )


def test_background_memoization_does_not_retain_cache_instances():
    cache = StylesCache()
    reference = weakref.ref(cache)
    cache.get_inner_outer(Color.parse("#101010"), Color.parse("#345678"))
    del cache
    gc.collect()
    assert reference() is None, "Color memoization retained a closed-widget render cache"


def test_background_cache_is_shared_by_values_and_configuration_is_idempotent():
    assert runtime.configure_styles_cache()
    before = StylesCache.get_inner_outer
    assert runtime.configure_styles_cache()
    assert StylesCache.get_inner_outer is before
    before.cache_clear()
    one, two = StylesCache(), StylesCache()
    base, background = Color.parse("#112233"), Color.parse("#445566")
    assert one.get_inner_outer(base, background) is two.get_inner_outer(base, background)
    info = before.cache_info()
    assert info.currsize == 1 and info.hits == 1 and info.maxsize == 1024


def test_unqualified_textual_versions_are_not_patched(monkeypatch):
    previous = StylesCache.get_inner_outer
    monkeypatch.setattr(runtime, "version", lambda _package: "7.6.0")
    assert not runtime.configure_styles_cache()
    assert StylesCache.get_inner_outer is previous
