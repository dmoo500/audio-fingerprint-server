"""One managed queue worker with explicit failures and optional cached enrichment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from fingerprint_server.async_utils import run_sync
from fingerprint_server.schemas import Submission, metadata_complete
from fingerprint_server.settings import Settings
from fingerprint_server.store import Store

LOGGER = logging.getLogger(__name__)


class EnrichmentError(Exception):
    """Unexpected or oversized metadata lookup response."""


class Worker:
    """Validate durable submissions before atomically publishing reference landmarks."""

    def __init__(self, store: Store, settings: Settings, client: httpx.AsyncClient) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.wakeup = asyncio.Event()
        self.next_lookup = 0.0

    async def run(self) -> None:
        """Process pending work; cancelled processing is recovered at the next startup."""
        while True:
            self.wakeup.clear()
            row = await run_sync(self.store.claim)
            if row is None:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=30)
                except TimeoutError:
                    pass
                continue
            try:
                submission = await run_sync(Submission.model_validate_json, row["document"])
                metadata = submission.metadata.model_dump(exclude_none=True)
                if not metadata_complete(metadata):
                    await run_sync(self.store.pending_metadata, row["id"])
                    continue
                if self.settings.musicbrainz_enabled and metadata.get("isrc"):
                    metadata["musicbrainz"] = await self.enrich(metadata["isrc"])
                    if (
                        metadata.get("recording_mbid")
                        and metadata["musicbrainz"].get("recording_mbid")
                        and metadata["recording_mbid"] != metadata["musicbrainz"]["recording_mbid"]
                    ):
                        metadata["musicbrainz"]["status"] = "conflict"
                await run_sync(self.publish, row["id"], submission, metadata)
            except (ValidationError, EnrichmentError) as err:
                LOGGER.error("Submission %s failed validation: %s", row["id"], type(err).__name__)
                await run_sync(
                    self.store.fail, row["id"], "Invalid reference or metadata response", None
                )
            except (httpx.HTTPError, TimeoutError) as err:
                retry = min(300 * row["attempts"], 3600) if row["attempts"] < 3 else None
                LOGGER.warning(
                    "Submission %s metadata lookup failed: %s", row["id"], type(err).__name__
                )
                await run_sync(self.store.fail, row["id"], "Metadata lookup unavailable", retry)

    def publish(self, submission_id: str, submission: Submission, metadata: dict[str, Any]) -> None:
        """Deduplicate exact acoustic references without overwriting conflicting metadata."""
        identity = {
            "format_id": submission.format_id,
            "windows": [window.model_dump() for window in submission.windows],
            "fingerprints": sorted(submission.fingerprints),
        }
        reference_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.store.finish(submission_id, reference_id, metadata, submission.fingerprints)

    async def enrich(self, isrc: str) -> dict[str, Any]:
        """Preserve supplied metadata and add only unambiguous ISRC lookup information."""
        isrc = isrc.upper()
        if (cached := await run_sync(self.store.cached_enrichment, isrc)) is not None:
            return cached
        await asyncio.sleep(max(0, self.next_lookup - time.monotonic()))
        self.next_lookup = time.monotonic() + 1.1
        async with self.client.stream(
            "GET",
            f"{str(self.settings.musicbrainz_base_url).rstrip('/')}/isrc/{isrc}",
            params={"fmt": "json"},
            headers={"User-Agent": f"MA-Fingerprint/0.1 ({self.settings.musicbrainz_contact})"},
        ) as response:
            if response.status_code == 404:
                result: dict[str, Any] = {"status": "not_found"}
            else:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2_000_000:
                        raise EnrichmentError("Metadata response exceeds the size limit")
                    body.extend(chunk)
                try:
                    document = await run_sync(json.loads, body)
                    if not isinstance(document, dict) or document.get("isrc") != isrc:
                        raise EnrichmentError("Invalid ISRC lookup")
                    recordings = document.get("recordings")
                    if not isinstance(recordings, list):
                        raise EnrichmentError("Invalid recording list")
                    if len(recordings) != 1:
                        result = {
                            "status": "ambiguous" if recordings else "not_found",
                            "candidate_count": len(recordings),
                        }
                    else:
                        recording = recordings[0]
                        if not isinstance(recording, dict) or not isinstance(
                            recording.get("id"), str
                        ):
                            raise EnrichmentError("Invalid recording identifier")
                        result = {
                            "status": "resolved",
                            "recording_mbid": str(UUID(recording["id"])),
                        }
                except (ValueError, UnicodeError) as err:
                    raise EnrichmentError("Invalid MusicBrainz JSON or identifier") from err
        await run_sync(self.store.cache_enrichment, isrc, result)
        return result
