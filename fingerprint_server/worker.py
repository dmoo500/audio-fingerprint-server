"""Orchestrate durable submissions and publish verified references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from fingerprint_server.async_utils import run_sync
from fingerprint_server.musicbrainz import (
    EnrichmentError,
    MusicBrainzService,
    MusicBrainzUnavailable,
    UnresolvableVerification,
    VerificationPending,
)
from fingerprint_server.schemas import Metadata, Submission, decode_submission, metadata_complete
from fingerprint_server.settings import Settings
from fingerprint_server.store import Store

LOGGER = logging.getLogger(__name__)
PUBLICATION_LOGGER = logging.getLogger("uvicorn.error.fingerprint.worker")


class Worker:
    """Validate durable submissions and atomically publish reference landmarks."""

    def __init__(self, store: Store, settings: Settings, client: httpx.AsyncClient) -> None:
        self.store = store
        self.settings = settings
        self.client = client
        self.musicbrainz = MusicBrainzService(store, settings, client)
        self.wakeup = asyncio.Event()

    async def run(self) -> None:
        """Process queued work and eligible pending rechecks until cancelled."""
        while True:
            self.wakeup.clear()
            await self.musicbrainz.wait_for_cooldown()
            row = await run_sync(self.store.claim)
            if row is None:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=30)
                except TimeoutError:
                    pass
                continue
            self.musicbrainz.lookup_call = "-"
            try:
                submission = await run_sync(decode_submission, row["document"])
                metadata = submission.metadata.model_dump(exclude_none=True)
                if self.settings.musicbrainz_enabled:
                    reference_id = row["reference_id"] or self.reference_id(submission)
                    try:
                        old = await run_sync(self.store.reference_metadata, reference_id)
                    except ValueError as err:
                        raise EnrichmentError("Retained reference metadata is invalid") from err
                    if old:
                        try:
                            Metadata.model_validate(
                                {key: old[key] for key in Metadata.model_fields if key in old}
                            )
                        except ValidationError as err:
                            raise EnrichmentError("Retained reference metadata is invalid") from err
                    if metadata.get("recording_mbid") and old.get("recording_mbid"):
                        if UUID(metadata["recording_mbid"]) != UUID(old["recording_mbid"]):
                            raise UnresolvableVerification(
                                "Conflicting retained recording identifier"
                            )
                    old_isrc = old.get("isrc")
                    new_isrc = metadata.get("isrc")
                    if old_isrc and new_isrc and old_isrc.upper() != new_isrc.upper():
                        old_candidate = dict(old)
                        if not old_candidate.get("recording_mbid") and metadata.get(
                            "recording_mbid"
                        ):
                            old_candidate["recording_mbid"] = metadata["recording_mbid"]
                        old_verified = await self.musicbrainz.verify(old_candidate)
                        new_candidate = dict(metadata)
                        if not new_candidate.get("recording_mbid") and old.get("recording_mbid"):
                            new_candidate["recording_mbid"] = old["recording_mbid"]
                        new_verified = await self.musicbrainz.verify(new_candidate)
                        if old_verified["recording_mbid"] != new_verified["recording_mbid"]:
                            raise UnresolvableVerification(
                                "Conflicting retained recording identifiers"
                            )
                    for key in ("title", "artists", "album", "duration", "isrc", "recording_mbid"):
                        if not self._present(metadata.get(key)) and self._present(old.get(key)):
                            metadata[key] = old[key]
                    metadata = await self.musicbrainz.verify(metadata)
                    for key in ("title", "artists", "album", "duration", "isrc", "recording_mbid"):
                        value = old.get(key)
                        if self._present(value):
                            metadata[key] = value
                    validated = submission.model_dump()
                    validated["metadata"] = metadata
                    try:
                        await run_sync(Submission.model_validate, validated)
                    except ValidationError as err:
                        raise UnresolvableVerification(
                            "Enriched metadata conflicts with fingerprint coverage"
                        ) from err
                    for key, value in old.items():
                        if key not in validated["metadata"]:
                            metadata[key] = value
                if not metadata_complete(metadata):
                    reason = "Incomplete metadata"
                    if self.settings.musicbrainz_enabled:
                        missing = [
                            key
                            for key in ("title", "artists", "album", "duration")
                            if not self._present(metadata.get(key))
                        ]
                        reason += " after MusicBrainz verification: " + ", ".join(missing)
                    await self._retain_pending(
                        row, reason, completed_check=self.settings.musicbrainz_enabled
                    )
                    continue
                await run_sync(self.publish, row["id"], submission, metadata, row["reference_id"])
                if self.settings.musicbrainz_enabled:
                    PUBLICATION_LOGGER.info(
                        "Submission %s verified with MusicBrainz and published", row["id"]
                    )
            except MusicBrainzUnavailable as err:
                delay = err.delay
                LOGGER.warning(
                    "MusicBrainz service cooldown: HTTP status=%s; retrying in %s seconds; "
                    "submission=%s; attempt preserved; api_call=%s",
                    err.status,
                    math.ceil(delay),
                    row["id"],
                    self.musicbrainz.lookup_call,
                )
                await run_sync(
                    self.store.defer_musicbrainz,
                    row["id"],
                    delay,
                    row["pending_recheck"],
                )
            except VerificationPending as err:
                await self._retain_pending(row, str(err), completed_check=True)
            except UnresolvableVerification as err:
                if self.settings.musicbrainz_enabled and self.settings.housekeeping_enabled:
                    if await run_sync(self.store.delete_submission, row["id"]):
                        LOGGER.warning(
                            "Submission %s deleted as unresolvable: %s; "
                            "completed_rechecks=%s; api_call=%s; references retained",
                            row["id"],
                            err,
                            row["pending_rechecks"],
                            self.musicbrainz.lookup_call,
                        )
                else:
                    await run_sync(self.store.pending_metadata, row["id"], str(err))
            except EnrichmentError as err:
                LOGGER.warning(
                    "Submission %s MusicBrainz response will be retried without using a check: "
                    "%s; api_call=%s",
                    row["id"],
                    err,
                    self.musicbrainz.lookup_call,
                )
                if row["pending_recheck"]:
                    await run_sync(
                        self.store.defer_pending_recheck,
                        row["id"],
                        str(err),
                        self.settings.pending_recheck_days * 86400,
                    )
                else:
                    await run_sync(self.store.pending_metadata, row["id"], str(err))
            except (ValidationError, ValueError) as err:
                LOGGER.error("Submission %s failed validation: %s", row["id"], type(err).__name__)
                await run_sync(
                    self.store.fail, row["id"], "Invalid reference or metadata response", None
                )
            except (httpx.HTTPError, TimeoutError) as err:
                if row["pending_recheck"]:
                    retry = min(300 * row["attempts"], 3600)
                    LOGGER.warning(
                        "Submission %s pending check unavailable: %s; HTTP status=%s; "
                        "attempt=%s; retry_seconds=%s; api_call=%s",
                        row["id"],
                        type(err).__name__,
                        self._http_status(err),
                        row["attempts"],
                        retry,
                        self.musicbrainz.lookup_call,
                    )
                    await run_sync(
                        self.store.defer_pending_recheck,
                        row["id"],
                        "Metadata lookup unavailable",
                        retry,
                    )
                else:
                    retry = min(300 * row["attempts"], 3600) if row["attempts"] < 3 else None
                    LOGGER.warning(
                        "Submission %s metadata lookup failed: %s; HTTP status=%s; "
                        "attempt=%s; retry_seconds=%s; api_call=%s",
                        row["id"],
                        type(err).__name__,
                        self._http_status(err),
                        row["attempts"],
                        retry,
                        self.musicbrainz.lookup_call,
                    )
                    await run_sync(self.store.fail, row["id"], "Metadata lookup unavailable", retry)

    def publish(
        self,
        submission_id: str,
        submission: Submission,
        metadata: dict[str, Any],
        reference_id: str | None = None,
    ) -> None:
        """Deduplicate exact acoustic references without overwriting conflicting metadata."""
        self.store.finish(
            submission_id,
            reference_id or self.reference_id(submission),
            metadata,
            submission.fingerprints,
            verified=self.settings.musicbrainz_enabled,
        )

    @staticmethod
    def reference_id(submission: Submission) -> str:
        """Return the unchanged acoustic identity of a submission."""
        identity = {
            "format_id": submission.format_id,
            "windows": [window.model_dump() for window in submission.windows],
            "fingerprints": sorted(submission.fingerprints),
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _present(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(isinstance(item, str) and item.strip() for item in value)
        return bool(value)

    async def _retain_pending(
        self, row: dict[str, Any], reason: str, completed_check: bool = False
    ) -> None:
        if completed_check and self.settings.musicbrainz_enabled:
            deleted = await run_sync(
                self.store.complete_pending_check,
                row["id"],
                reason,
                row["pending_recheck"],
            )
            if deleted:
                LOGGER.warning(
                    "Submission %s deleted after %s unresolved completed MusicBrainz checks; "
                    "reason=%s; api_call=%s; references and landmarks retained",
                    row["id"],
                    self.settings.pending_max_rechecks,
                    reason,
                    self.musicbrainz.lookup_call,
                )
            else:
                LOGGER.info(
                    "Submission %s remains pending after MusicBrainz check; "
                    "completed_rechecks=%s; next_check_days=%s; reason=%s; api_call=%s",
                    row["id"],
                    row["pending_rechecks"]
                    + int(row["pending_recheck"] and self.settings.housekeeping_enabled),
                    self.settings.pending_recheck_days,
                    reason,
                    self.musicbrainz.lookup_call,
                )
        else:
            await run_sync(self.store.pending_metadata, row["id"], reason)

    @staticmethod
    def _http_status(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            return str(error.response.status_code)
        return "unavailable"
