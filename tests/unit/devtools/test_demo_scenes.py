"""demokit.scenes and demokit.badges: the scene table and the README badges."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from demokit import badges, scenes


def test_nine_ordered_scenes_within_the_gif_budget() -> None:
    scenes.check()
    assert [s.id[:2] for s in scenes.SCENES] == [f"{i:02d}" for i in range(1, 10)]
    assert scenes.GIF_MIN_S <= scenes.total_s() <= scenes.GIF_MAX_S
    assert all(len(s.alt.split()) >= 6 for s in scenes.SCENES)
    assert "fake GitHub" in scenes.SCENES[5].subtitle  # the LIVE scene says what LIVE ran against


def test_badges_are_well_formed_deterministic_svg() -> None:
    first = badges.all_badges()
    assert first == badges.all_badges()
    assert set(first) == {"license.svg", "python.svg", "ci.svg", "dry-run.svg"}
    texts = set()
    for svg in first.values():
        root = ET.fromstring(svg)  # noqa: S314 - our own generated SVG
        assert root.get("role") == "img"
        texts |= {t.text for t in root.iter("{http://www.w3.org/2000/svg}text")}
    assert {"Apache-2.0", "3.12", "manual run", "on by default"} <= texts
