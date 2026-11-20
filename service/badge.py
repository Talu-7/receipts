"""Shields-style SVG badge for a check's verdict."""
from __future__ import annotations

from html import escape

STYLES = {
    "reproduced": ("reproduced", "#1f9d55"),
    "partially_reproduced": ("partially reproduced", "#d9a400"),
    "discrepancy_found": ("discrepancy found", "#d6336c"),
    "inconclusive": ("inconclusive", "#6b7280"),
    "queued": ("queued", "#3b82f6"),
    "running": ("checking…", "#3b82f6"),
    "unknown": ("not found", "#9ca3af"),
}


def _w(text: str) -> int:
    # Verdana 11px averages ~6.5px per glyph; good enough for a badge
    return int(len(text) * 6.5 + 12)


def render(state: str, label: str = "receipts") -> str:
    value, color = STYLES.get(state, STYLES["unknown"])
    lw, vw = _w(label), _w(value)
    total = lw + vw
    l, v = escape(label), escape(value)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{l}: {v}">
<title>{l}: {v}</title>
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#24292f"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/><rect width="{total}" height="20" fill="url(#s)"/></g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
<text x="{lw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{l}</text><text x="{lw / 2:.1f}" y="14">{l}</text>
<text x="{lw + vw / 2:.1f}" y="15" fill="#010101" fill-opacity=".3">{v}</text><text x="{lw + vw / 2:.1f}" y="14">{v}</text>
</g></svg>"""


def markdown(base_url: str, check_id: str) -> str:
    return f"[![Receipts-verified]({base_url}/badge/{check_id}.svg)]({base_url}/receipts/{check_id})"
