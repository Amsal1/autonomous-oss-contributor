"""README showcase (docs/media): section order, relative media paths, alt texts, GIF limits, the banner and
mermaid hygiene. GitHub's own rendering (mermaid runs client-side) is checked by eye after a push."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
MEDIA = ROOT / "docs" / "media"
SVG = "{http://www.w3.org/2000/svg}"
GIF_MAX = 6_000_000


def _targets() -> list[str]:
    html = re.findall(r'(?:src|href)="([^"]+)"', README)
    markdown = re.findall(r"\]\(([^)\s]+)\)", README)
    return [t for t in html + markdown if not t.startswith(("http://", "https://", "#", "mailto:"))]


def test_section_order() -> None:
    marks = ['src="docs/media/banner.svg"', 'src="docs/media/badges/license.svg"', "**A self-hosted bot",
             'src="docs/media/demo.gif"', "\n## Why\n", "\n## How it works\n", "\n## Safety model\n",
             "\n## Quickstart\n"]  # fmt: skip
    positions = [README.index(m) for m in marks]
    assert positions == sorted(positions)
    assert README.lstrip().startswith('<p align="center">\n  <img src="docs/media/banner.svg"')


def test_every_relative_link_and_image_exists() -> None:
    targets = _targets()
    assert "docs/media/demo.gif" in targets
    for target in targets:
        assert (ROOT / target.split("#", 1)[0]).exists(), target


def test_every_image_has_a_descriptive_alt() -> None:
    images = re.findall(r"<img\s[^>]*>", README)
    assert len(images) >= 9
    for tag in images:
        src = re.search(r'src="([^"]+)"', tag)
        alt = re.search(r'alt="([^"]+)"', tag)
        assert src and alt, tag
        words = 2 if "/badges/" in src.group(1) else 6
        assert len(alt.group(1).split()) >= words, tag
    assert "![" not in README  # markdown images carry no width; the README uses <img> only


def test_demo_gif_limits() -> None:
    data = (MEDIA / "demo.gif").read_bytes()
    assert data[:6] == b"GIF89a" and len(data) <= GIF_MAX
    width, _height = struct.unpack("<HH", data[6:10])
    assert width == 720
    assert b"NETSCAPE2.0" in data[:2048]  # the looping extension
    assert (MEDIA / "demo.mp4").stat().st_size <= GIF_MAX


def test_manifest_lists_the_committed_outputs() -> None:
    manifest = json.loads((MEDIA / "demo.manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["scenes"]) == 9 and 12 <= manifest["outputs"]["demo.gif"]["duration_s"] <= 20
    for rel, info in manifest["outputs"].items():
        assert (MEDIA / rel).stat().st_size == info["size"], rel
    stills = {p.name for p in (MEDIA / "stills").glob("*.png")}
    assert stills == {rel.split("/", 1)[1] for rel in manifest["outputs"] if rel.startswith("stills/")}


def test_banner_and_badges_are_accessible_svg() -> None:
    banner = ET.parse(MEDIA / "banner.svg").getroot()  # noqa: S314 - a tracked file of this repo
    assert banner.get("role") == "img"
    assert banner.find(f"{SVG}title") is not None and banner.find(f"{SVG}desc") is not None
    text = (MEDIA / "banner.svg").read_text(encoding="utf-8")
    assert "<script" not in text and "href" not in text
    for name in ("license", "python", "ci", "dry-run"):
        badge = ET.parse(MEDIA / "badges" / f"{name}.svg").getroot()  # noqa: S314 - a tracked file of this repo
        assert badge.find(f"{SVG}title") is not None


def test_mermaid_uses_plain_flowcharts_only() -> None:
    blocks = re.findall(r"```mermaid\n(.*?)```", README, flags=re.S)
    assert len(blocks) == 2
    for block in blocks:
        assert block.startswith("flowchart ")
        assert "<script" not in block and not re.search(r"^\s*click\s", block, flags=re.M)
