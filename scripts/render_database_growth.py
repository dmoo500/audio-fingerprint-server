"""Render the SQLite and PostgreSQL growth SVGs from documented snapshot measurements."""

from __future__ import annotations

import json
import math
from html import escape
from pathlib import Path


def render(chart: dict) -> str:
    """Render a labeled multi-scale chart without inferring unmeasured data."""
    snapshots = chart["snapshots"]
    colors = ("#1769aa", "#1b6d38", "#b65c00")
    series = (
        ("size_mib", "MiB", chart["size_label"]),
        ("references", "References", "Stored references"),
        ("landmarks", "Landmarks", "Stored landmarks"),
    )
    height = max(480, 200 + len(snapshots) * 100)
    bottom = height - 90
    top = 120
    xs = [340 + index * 480 / max(1, len(snapshots) - 1) for index in range(len(snapshots))]
    description = "; ".join(
        f"{point['label']}: {point['size_mib']:,.2f} MiB, "
        f"{point['references']:,} stored references, {point['landmarks']:,} landmarks."
        for point in snapshots
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1300" height="{height}" '
        f'viewBox="0 0 1300 {height}" role="img" aria-labelledby="title description">',
        f'<title id="title">{escape(chart["title"])}</title>',
        f'<desc id="description">{escape(description)}</desc>',
        f'<rect width="1300" height="{height}" fill="#ffffff"/>',
        '<g font-family="sans-serif" fill="#222222">',
        f'<text x="650" y="32" font-size="23" font-weight="bold" text-anchor="middle">'
        f"{escape(chart['title'])}</text>",
        f'<text x="650" y="58" font-size="14" text-anchor="middle">'
        f"{escape(chart['subtitle'])}</text>",
        '<text x="650" y="82" font-size="13" text-anchor="middle">'
        "Each series has its own vertical scale; snapshots are not a capacity forecast</text>",
    ]
    for fraction in (0, 0.5, 1):
        y = bottom - fraction * (bottom - top)
        lines.append(f'<path d="M300 {y}H850" stroke="#dddddd"/>')
    for index, ((key, unit, label), color) in enumerate(zip(series, colors, strict=True)):
        maximum = max(point[key] for point in snapshots)
        quantum = 10 ** math.floor(math.log10(maximum))
        ceiling = math.ceil(maximum / quantum) * quantum
        axis_x = (55, 145, 245)[index]
        lines.append(
            f'<text x="{axis_x}" y="106" fill="{color}" font-size="13" '
            f'text-anchor="middle">{unit}</text>'
        )
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * (bottom - top)
            lines.append(
                f'<text x="{axis_x}" y="{y + 4}" fill="{color}" font-size="12" '
                f'text-anchor="middle">{ceiling * fraction:,.0f}</text>'
            )
        points = [
            (x, bottom - point[key] / ceiling * (bottom - top))
            for x, point in zip(xs, snapshots, strict=True)
        ]
        for number in range(1, len(points)):
            (x1, y1), (x2, y2) = points[number - 1], points[number]
            different_basis = key == "size_mib" and any(
                point.get("size_basis") == "file" for point in snapshots[number - 1 : number + 1]
            )
            dash = ' stroke-dasharray="6 4"' if different_basis else ""
            lines.append(
                f'<path d="M{x1:.2f} {y1:.2f}L{x2:.2f} {y2:.2f}" fill="none" '
                f'stroke="{color}" stroke-width="3"{dash}/>'
            )
        for (x, y), point in zip(points, snapshots, strict=True):
            fill = "#ffffff" if key == "size_mib" and point.get("size_basis") == "file" else color
            lines.append(
                f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{fill}" '
                f'stroke="{color}" stroke-width="2">'
                f"<title>{escape(point['label'])}: {point[key]:,}</title></circle>"
            )
        legend_y = 120 + index * (50 + len(snapshots) * 22)
        lines.append(
            f'<text x="890" y="{legend_y}" fill="{color}" font-size="15" '
            f'font-weight="bold">{escape(label)}</text>'
        )
        for number, point in enumerate(snapshots):
            value = f"{point[key]:,.2f}" if key == "size_mib" else f"{point[key]:,}"
            if key == "size_mib" and point.get("size_basis") == "file":
                value += " (file)"
            lines.append(
                f'<text x="890" y="{legend_y + 24 + number * 22}" font-size="14">'
                f"{escape(point['label'])}: {value}</text>"
            )
    for x, point in zip(xs, snapshots, strict=True):
        lines.append(
            f'<text x="{x}" y="{bottom + 30}" text-anchor="middle" font-size="13">'
            f"{escape(point['label'])}</text>"
        )
    lines.extend(
        [
            f'<text x="575" y="{height - 24}" font-size="13" text-anchor="middle">'
            "Measured snapshots; reference counts include unpublished references</text>",
            "</g></svg>",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    docs = Path(__file__).resolve().parents[1] / "docs"
    charts = json.loads((docs / "database-growth.json").read_text())
    for chart in charts.values():
        (docs / chart["output"]).write_text(render(chart))
