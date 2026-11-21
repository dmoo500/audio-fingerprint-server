"""Prepare SVG line charts from hourly admin samples."""

from datetime import UTC, datetime
from typing import Any

from fingerprint_server.database.metrics import HOUR

RECORDINGS = (("recordings", "Searchable recordings", "#2f9e44"),)
QUEUE = (
    ("queued", "Queued", "#4263eb"),
    ("processing", "Processing", "#ae3ec9"),
    ("pending_metadata", "Pending metadata", "#d08800"),
    ("manual_review", "Manual MusicBrainz review", "#d63939"),
)
ARTWORK = (("cached_artwork", "Cached covers", "#0c8599"),)


def chart(
    samples: list[dict[str, Any]],
    hour: int,
    columns: tuple[tuple[str, str, str], ...],
    secondary: tuple[tuple[str, str, str], ...] = (),
) -> dict[str, Any]:
    """Build a time-scaled chart with visible gaps and the last measured values."""
    if not samples:
        return {}
    start = samples[0]["hour"]
    maximum = max(1, max(row[key] for row in samples for key, _, _ in columns))
    secondary_values = [
        row[key] for row in samples for key, _, _ in secondary if row.get(key) is not None
    ]
    secondary_maximum = max(1, max(secondary_values)) if secondary_values else None
    series = []
    for key, label, color in (*columns, *secondary):
        scale = secondary_maximum if (key, label, color) in secondary else maximum
        values = [sample[key] for sample in samples if sample.get(key) is not None]
        if scale is None or not values:
            continue
        path: list[str] = []
        dots: list[tuple[float, float]] = []
        previous = start - 2
        for sample in samples:
            bucket = sample["hour"]
            if sample.get(key) is None:
                previous = bucket - 1
                continue
            x = round(600 * (bucket - start) / max(1, hour - start), 2)
            y = round(100 * (1 - sample[key] / scale), 2)
            if bucket != previous + 1:
                path.append(f"M{x},{y}")
                dots.append((x, y))
            else:
                path.append(f"L{x},{y}")
            previous = bucket
        series.append(
            {
                "label": label,
                "color": color,
                "path": " ".join(path),
                "dots": dots,
                "last": values[-1],
                "axis": "right" if (key, label, color) in secondary else "left",
            }
        )
    return {
        "series": series,
        "maximum": maximum,
        "secondary_maximum": secondary_maximum,
        "start": datetime.fromtimestamp(start * HOUR, UTC).strftime("%d %b %H:%M"),
        "end": datetime.fromtimestamp(hour * HOUR, UTC).strftime("%d %b %H:%M"),
        "last_at": datetime.fromtimestamp(samples[-1]["hour"] * HOUR, UTC).strftime("%d %b %H:%M"),
    }
