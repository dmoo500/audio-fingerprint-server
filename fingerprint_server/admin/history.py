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


def chart(
    samples: list[dict[str, Any]], hour: int, columns: tuple[tuple[str, str, str], ...]
) -> dict[str, Any]:
    """Build a time-scaled chart with visible gaps and the last measured values."""
    if not samples:
        return {}
    start = samples[0]["hour"]
    maximum = max(1, max(row[key] for row in samples for key, _, _ in columns))
    series = []
    for key, label, color in columns:
        path: list[str] = []
        dots: list[tuple[float, float]] = []
        previous = start - 2
        for sample in samples:
            bucket = sample["hour"]
            x = round(600 * (bucket - start) / max(1, hour - start), 2)
            y = round(100 * (1 - sample[key] / maximum), 2)
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
                "last": samples[-1][key],
            }
        )
    return {
        "series": series,
        "maximum": maximum,
        "start": datetime.fromtimestamp(start * HOUR, UTC).strftime("%d %b %H:%M"),
        "end": datetime.fromtimestamp(hour * HOUR, UTC).strftime("%d %b %H:%M"),
        "last_at": datetime.fromtimestamp(samples[-1]["hour"] * HOUR, UTC).strftime("%d %b %H:%M"),
    }
