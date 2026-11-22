"""Backend-independent coverage, matching and availability decisions."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from audio_fingerprint import HOP_SIZE, SAMPLE_RATE

from fingerprint_server.schemas import ISRC_PATTERN, metadata_complete, metadata_searchable


def can_add_recording_mbid(existing: object, incoming: Mapping[str, Any]) -> bool:
    """Require the same retained identity before adding a verified recording ID."""
    return (
        isinstance(existing, dict)
        and not existing.get("recording_mbid")
        and all(existing.get(key) == incoming.get(key) for key in ("title", "artists", "duration"))
        and (
            not existing.get("isrc")
            or str(existing["isrc"]).upper() == str(incoming.get("isrc")).upper()
        )
    )


def has_identifier(metadata: object) -> bool:
    """Check for a usable retained ISRC or recording UUID."""
    if not isinstance(metadata, dict):
        return False
    isrc = metadata.get("isrc")
    if isinstance(isrc, str) and re.fullmatch(ISRC_PATTERN, isrc):
        return True
    mbid = metadata.get("recording_mbid")
    if isinstance(mbid, str):
        try:
            UUID(mbid)
        except ValueError:
            return False
        return True
    return False


def initial_submission_state(
    document: str, musicbrainz_enabled: bool, housekeeping_enabled: bool
) -> tuple[str, str | None]:
    """Choose pending retention or worker admission using the shared publication policy."""
    try:
        payload = json.loads(document)
    except (ValueError, UnicodeError):
        payload = None
    pending = (
        isinstance(payload, dict)
        and "metadata" in payload
        and not metadata_complete(payload["metadata"])
    )
    if pending and musicbrainz_enabled and isinstance(payload["metadata"], dict):
        pending = not (
            has_identifier(payload["metadata"]) or metadata_searchable(payload["metadata"])
        )
    if pending and musicbrainz_enabled and housekeeping_enabled:
        pending = False
    error = (
        "MusicBrainz identifier unavailable"
        if pending and musicbrainz_enabled
        else "Incomplete metadata"
        if pending
        else None
    )
    return ("pending_metadata" if pending else "queued"), error


def full_coverage(document: str | None, metadata: str) -> bool:
    """Check coverage without trusting malformed historical documents."""
    if document is None:
        return False
    try:
        windows = json.loads(document)["windows"]
        duration = json.loads(metadata)["duration"]
        end = 0
        contiguous = bool(windows)
        for window in windows:
            if window["start_sample"] > end:
                contiguous = False
            end = max(end, window["start_sample"] + window["sample_count"])
        return contiguous and end == round(duration * SAMPLE_RATE)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def rank_matches(
    query: list[tuple[str, int]],
    rows: Sequence[Mapping[str, Any]],
    read_metadata: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Rank offset-consistent candidates without counting one query pair twice."""
    recording_keys = {row["reference_id"]: row["recording_key"] for row in rows}
    groups: dict[tuple[str, int], set[int]] = defaultdict(set)
    for row in sorted(rows, key=lambda row: (row["idx"], row["delta"])):
        groups[(row["reference_id"], row["delta"])].add(row["idx"])
    best: dict[str, tuple[int, set[int], str | None]] = {}
    for (reference_id, delta), indices in groups.items():
        if reference_id not in best or len(indices) > len(best[reference_id][1]):
            best[reference_id] = (delta, indices, recording_keys[reference_id])
    ranked = sorted(best.items(), key=lambda entry: (-len(entry[1][1]), entry[0]))
    results = []
    accepted: list[tuple[int, set[int], dict[str, Any], str | None]] = []
    seen_recordings: set[str] = set()
    for reference_id, (delta, indices, recording_key) in ranked:
        if recording_key is not None and recording_key in seen_recordings:
            continue
        offsets = [query[index][1] for index in indices]
        metadata = json.loads(read_metadata(reference_id))
        # Old sampled and new full references can describe the same aligned recording.
        identified = (
            metadata.get("recording_mbid")
            or metadata.get("isrc")
            or (metadata.get("title") and metadata.get("artists"))
        )
        if (
            identified
            and recording_key is None
            and any(
                other_recording_key is None
                and other_delta == delta
                and other_metadata == metadata
                and len(indices & other_indices) >= 0.9 * min(len(indices), len(other_indices))
                for other_delta, other_indices, other_metadata, other_recording_key in accepted
            )
        ):
            continue
        accepted.append((delta, indices, metadata, recording_key))
        if recording_key is not None:
            seen_recordings.add(recording_key)
        results.append(
            {
                "reference_id": reference_id,
                "matched_landmarks": len(indices),
                "query_landmarks": len(query),
                "match_fraction": len(indices) / len(query),
                "offset_seconds": delta * HOP_SIZE / SAMPLE_RATE,
                "matched_span_seconds": (max(offsets) - min(offsets)) * HOP_SIZE / SAMPLE_RATE,
                "metadata": metadata,
            }
        )
        if len(results) == 2:
            break
    return results


def coverage_statistics(
    rows: Iterable[Mapping[str, Any]], uploads: dict[str, int]
) -> dict[str, Any]:
    """Count acoustic references and logical recordings from a consistent snapshot."""
    coverage = {"total": 0, "partial": 0, "full": 0, "excluded": 0}
    recordings: dict[str, dict[str, bool]] = {}
    for row in rows:
        key = row["recording_key"] or f"reference:{row['id']}"
        logical = recordings.setdefault(key, {"searchable": False, "full": False})
        if not row["publication_ready"]:
            coverage["excluded"] += 1
            continue
        full = full_coverage(row["document"], row["metadata"])
        coverage["full" if full else "partial"] += 1
        coverage["total"] += 1
        logical["searchable"] = True
        logical["full"] |= full
    logical_coverage = {"total": 0, "partial": 0, "full": 0, "excluded": 0}
    for recording in recordings.values():
        if not recording["searchable"]:
            logical_coverage["excluded"] += 1
        else:
            logical_coverage["total"] += 1
            logical_coverage["full" if recording["full"] else "partial"] += 1
    return {"references": coverage, "recordings": logical_coverage, "submissions": uploads}


def resolve_availability(
    items: list[dict[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> list[str | None]:
    """Resolve supplied recording evidence to a verified logical identity."""
    from fingerprint_server.musicbrainz import recording_matches

    resolved: list[str | None] = []
    for item in items:
        key = UUID(item["recording_mbid"]).hex if item.get("recording_mbid") else None
        isrc = item.get("isrc", "").upper() if item.get("isrc") else None
        found = None
        if key or isrc:
            for row in rows:
                metadata = json.loads(row["metadata"])
                if key and row["recording_key"] != key:
                    continue
                if isrc and str(metadata.get("isrc") or "").upper() != isrc:
                    continue
                if not key:
                    if not item.get("duration"):
                        continue
                    candidate = {**metadata, "length": metadata.get("duration", 0) * 1000}
                    if not recording_matches(candidate, item):
                        continue
                found = row["recording_key"]
                break
        resolved.append(found)
    return resolved
