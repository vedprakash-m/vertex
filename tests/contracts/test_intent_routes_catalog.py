from __future__ import annotations

from scripts.generate_intent_routes import OUTPUT_PATH, build_intent_route_catalog, render_intent_route_catalog


def test_intent_routes_catalog_is_current() -> None:
    expected = render_intent_route_catalog(build_intent_route_catalog())
    actual = OUTPUT_PATH.read_text(encoding="utf-8")
    assert actual == expected
