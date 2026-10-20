"""Flat SVG badges for the README, generated locally (no third-party badge service, no request)."""

from __future__ import annotations

import html

# (file stem, label, value, value colour)
BADGES: tuple[tuple[str, str, str, str], ...] = (
    ("license", "license", "Apache-2.0", "#2f7fe0"),
    ("python", "python", "3.12", "#3572a5"),
    ("ci", "CI", "manual run", "#5f7d64"),
    ("dry-run", "dry-run", "on by default", "#2e9d4f"),
)
CHAR_W = 6.6  # px per character at 11px Verdana-like text (a fixed estimate keeps the output deterministic)
PAD = 6


def _width(text: str) -> int:
    return round(len(text) * CHAR_W) + 2 * PAD


def svg(label: str, value: str, colour: str) -> str:
    lw, vw = _width(label), _width(value)
    total = lw + vw
    title = html.escape(f"{label}: {value}", quote=True)
    lab, val = html.escape(label, quote=False), html.escape(value, quote=False)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" viewBox="0 0 {total} 20" '
        f'role="img" aria-label="{title}">\n'
        f"  <title>{html.escape(f'{label}: {value}', quote=False)}</title>\n"
        f'  <clipPath id="r"><rect width="{total}" height="20" rx="3"/></clipPath>\n'
        f'  <g clip-path="url(#r)">\n'
        f'    <rect width="{lw}" height="20" fill="#555"/>\n'
        f'    <rect x="{lw}" width="{vw}" height="20" fill="{colour}"/>\n'
        f"  </g>\n"
        f'  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">\n'
        f'    <text x="{lw / 2:.1f}" y="14">{lab}</text>\n'
        f'    <text x="{lw + vw / 2:.1f}" y="14">{val}</text>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def all_badges() -> dict[str, str]:
    return {f"{stem}.svg": svg(label, value, colour) for stem, label, value, colour in BADGES}
