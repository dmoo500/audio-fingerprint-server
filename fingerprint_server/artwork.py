"""Durable album artwork resolved only by the ingestion worker."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import httpx

from fingerprint_server.artwork_store import ArtworkStore as ArtworkStore
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database import Storage
from fingerprint_server.http_utils import retry_after, trusted_image
from fingerprint_server.musicbrainz import (
    EnrichmentError,
    MusicBrainzService,
    MusicBrainzUnavailable,
)

LOGGER = logging.getLogger(__name__)
PREWARM_INTERVAL = 15
RELEASE_FALLBACK_LIMIT = 3


@dataclass(frozen=True)
class ArtworkResult:
    """Artwork URL and the reason for its selection or absence."""

    url: str | None
    reason: str


def normalized(value: str) -> str:
    """Compare album names without punctuation, case or accent differences."""
    value = re.sub(
        r"\s*(?:[\[(](?:(?:\d+(?:st|nd|rd|th)\s+)?anniversary(?:\s+deluxe)?|"
        r"deluxe|special|expanded|collector'?s|remaster(?:ed)?|bonus track)"
        r"(?:\s+(?:edition|version))?[\])]|[\[(](?:ep|single)[\])]|"
        r"-\s+(?:ep|single))\s*$",
        "",
        unicodedata.normalize("NFKC", value),
        flags=re.IGNORECASE,
    )
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())


def exact_name(value: str) -> str:
    """Compare release names without removing edition labels."""
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())


def earliest_official(items: list[dict[str, Any]], key: str) -> str | None:
    """Select a uniquely earliest official release or release group."""
    dates: dict[str, date] = {}
    official = [item for item in items if item.get("status") == "Official"]
    for item in official:
        value = item.get("date")
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None
        try:
            release_date = date.fromisoformat(value)
        except ValueError:
            return None
        identifier = item[key]
        dates[identifier] = min(dates.get(identifier, release_date), release_date)
    if not dates or set(dates) != {item[key] for item in items}:
        return None
    first = min(dates.values())
    earliest = [identifier for identifier, value in dates.items() if value == first]
    return earliest[0] if len(earliest) == 1 else None


class ArtworkResolver:
    """Resolve matching album releases using the worker's shared MusicBrainz pacing."""

    def __init__(self, store: Storage, musicbrainz: MusicBrainzService) -> None:
        self.queue = ArtworkStore(store)
        self.musicbrainz = musicbrainz
        self.next_cover = 0.0
        self.next_prewarm = 0.0

    async def process_one(self, *, prewarm: bool = False) -> None:
        """Complete one lookup, respecting archive backoff and the idle work budget."""
        now = time.monotonic()
        if self.next_cover > now or (prewarm and self.next_prewarm > now):
            return
        if prewarm:
            self.next_prewarm = now + PREWARM_INTERVAL
        job = await run_sync(self.queue.next, prewarm=prewarm)
        if job is None:
            return
        reference = job["reference_key"]
        try:
            metadata = json.loads(job["metadata"])
            mbid = str(UUID(metadata["recording_mbid"]))
            album = metadata["album"]
            result = await self.musicbrainz.lookup("artwork", mbid)
            resolved = (
                ArtworkResult(None, "recording_not_found")
                if result.get("status") == "not found"
                else await self.album_cover(
                    album,
                    result["releases"],
                    release_hint=metadata.get("release_mbid"),
                    group_hint=metadata.get("release_group_mbid"),
                    complete=result["complete"],
                )
            )
        except MusicBrainzUnavailable as err:
            LOGGER.warning("Artwork lookup deferred by MusicBrainz: HTTP %s", err.status)
            await run_sync(
                self.queue.finish, reference, None, failed=True, reason="upstream_unavailable"
            )
            return
        except (httpx.HTTPError, EnrichmentError, ValueError, KeyError, TypeError) as err:
            LOGGER.warning("Artwork lookup failed: %s", type(err).__name__)
            await run_sync(self.queue.finish, reference, None, failed=True, reason="upstream_error")
            return
        LOGGER.info("Artwork lookup completed: reference=%s reason=%s", reference, resolved.reason)
        await run_sync(
            self.queue.finish,
            reference,
            resolved.url,
            reason=resolved.reason,
            failed=resolved.reason == "incomplete_releases",
        )

    async def album_cover(
        self,
        album: str,
        releases: list[dict[str, Any]],
        *,
        release_hint: str | None = None,
        group_hint: str | None = None,
        complete: bool = True,
    ) -> ArtworkResult:
        """Find artwork using recording-associated editions or an unambiguous album group."""
        if not album:
            return ArtworkResult(None, "album_not_found")
        requested = album
        album = normalized(album)
        exact_edition = bool(
            re.search(
                r"\(\s*(?:deluxe\s+version|expanded\s+edition)\s*\)\s*$",
                requested,
                re.IGNORECASE,
            )
        )
        single_ep = re.search(r"(?:-\s*|\(\s*)(single|ep)\s*\)?\s*$", requested, re.IGNORECASE)
        release_hint = str(UUID(release_hint)) if release_hint else None
        group_hint = str(UUID(group_hint)) if group_hint else None
        hinted = next((item for item in releases if item.get("id") == release_hint), None)
        if release_hint:
            if hinted is None:
                return ArtworkResult(
                    None, "release_hint_not_found" if complete else "incomplete_releases"
                )
            if group_hint and hinted["group"] != group_hint:
                return ArtworkResult(None, "conflicting_release_hint")
            group_hint = hinted["group"]
        if group_hint and not any(item["group"] == group_hint for item in releases):
            return ArtworkResult(
                None, "release_hint_not_found" if complete else "incomplete_releases"
            )
        if exact_edition:
            exact = [
                item
                for item in releases
                if item.get("id")
                and exact_name(item["title"]) == exact_name(requested)
                and (not group_hint or item["group"] == group_hint)
                and (not release_hint or item["id"] == release_hint)
            ]
            if not complete and not release_hint:
                return ArtworkResult(None, "incomplete_releases")
            if not exact:
                return ArtworkResult(None, "album_not_found")
            release_ids = {item["id"] for item in exact}
            selected = (
                next(iter(release_ids)) if len(release_ids) == 1 else earliest_official(exact, "id")
            )
            if selected is None:
                return ArtworkResult(None, "ambiguous_album")
            if url := await self.cover(selected, release=True):
                return ArtworkResult(url, "ready_release")
            return ArtworkResult(None, "no_cover")
        matching = [release for release in releases if normalized(release["title"]) == album]
        candidates = {release["group"] for release in matching}
        candidates.update(
            release["group"]
            for release in releases
            if normalized(release.get("group_title", "")) == album
        )
        if not group_hint:
            if not complete:
                return ArtworkResult(None, "incomplete_releases")
            if not candidates:
                return ArtworkResult(None, "album_not_found")
            if single_ep:
                expected_type = single_ep.group(1).upper()
                expected_type = "Single" if expected_type == "SINGLE" else expected_type
                typed = [
                    item
                    for item in releases
                    if item["group"] in candidates and item.get("group_type") == expected_type
                ]
                if any(
                    item["group"] in candidates and not item.get("group_type") for item in releases
                ):
                    return ArtworkResult(None, "ambiguous_album")
                if not typed:
                    return ArtworkResult(None, "album_not_found")
                typed_groups = {item["group"] for item in typed}
                if len(typed_groups) == 1:
                    candidates = typed_groups
                else:
                    first_group = earliest_official(typed, "group")
                    if first_group is None:
                        return ArtworkResult(None, "ambiguous_album")
                    candidates = {first_group}
            elif len(candidates) != 1:
                return ArtworkResult(None, "ambiguous_album")
            group_hint = next(iter(candidates))
        if release_hint and (url := await self.cover(release_hint, release=True)):
            return ArtworkResult(url, "ready_release")
        if url := await self.cover(group_hint):
            return ArtworkResult(url, "ready_group")
        editions = {
            item["id"]: item
            for item in releases
            if item["group"] == group_hint and item.get("id") and item["id"] != release_hint
        }
        ranked = sorted(
            editions.values(),
            key=lambda item: (
                not item.get("front", False),
                item.get("status") != "Official",
                normalized(item["title"]) != album,
                item.get("date") or "9999",
                item["id"],
            ),
        )
        for edition in ranked[:RELEASE_FALLBACK_LIMIT]:
            if url := await self.cover(edition["id"], release=True):
                return ArtworkResult(url, "ready_release")
        return ArtworkResult(
            None,
            "incomplete_releases"
            if not complete
            else "search_limit"
            if len(ranked) > RELEASE_FALLBACK_LIMIT
            else "no_cover",
        )

    async def cover(self, identifier: str, *, release: bool = False) -> str | None:
        """Resolve the archive redirect without downloading the image."""
        kind = "release" if release else "release-group"
        key = f"coverart:v1:{kind}:{identifier}"
        cached = await run_sync(self.queue.store.cached_enrichment, key)
        if cached is not None:
            return cached["url"]
        await asyncio.sleep(max(0, self.next_cover - time.monotonic()))
        self.next_cover = time.monotonic() + 1.1
        response = await self.musicbrainz.client.head(
            f"https://coverartarchive.org/{kind}/{identifier}/front-500",
            headers={
                "User-Agent": f"MA-Fingerprint/0.1 "
                f"({self.musicbrainz.settings.musicbrainz_contact})"
            },
            follow_redirects=False,
        )
        if response.status_code == 404:
            await run_sync(self.queue.store.cache_enrichment, key, {"url": None})
            return None
        if response.status_code in (301, 302, 307, 308):
            url = response.headers.get("location", "")
            if trusted_image(url):
                await run_sync(self.queue.store.cache_enrichment, key, {"url": url})
                return url
            raise ValueError("Untrusted cover redirect")
        if response.status_code in (429, 503):
            delay = max(60, retry_after(response.headers.get("retry-after")))
            self.next_cover = time.monotonic() + delay
        response.raise_for_status()
        raise ValueError("Cover archive did not return an image redirect")
