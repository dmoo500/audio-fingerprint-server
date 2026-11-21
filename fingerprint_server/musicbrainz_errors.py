"""Typed outcomes of MusicBrainz verification and upstream failures."""

from typing import Any


class EnrichmentError(Exception):
    """Malformed or unusable MusicBrainz data that may be retried later."""


class VerificationPending(EnrichmentError):
    """A valid MusicBrainz result cannot yet resolve a unique complete recording."""


class RecordingReviewRequired(VerificationPending):
    """Multiple recordings matched the submission metadata."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__("MusicBrainz recording requires manual selection")
        self.candidates = candidates


class UnresolvableVerification(EnrichmentError):
    """The supplied identifiers are absent or definitively conflict with MusicBrainz."""


class MissingIdentifierVerification(UnresolvableVerification):
    """Neither a recording identifier nor an ISRC is available."""


class MusicBrainzUnavailable(Exception):
    """A MusicBrainz response requiring a service-wide cooldown."""

    def __init__(self, status: int, delay: float) -> None:
        self.status = status
        self.delay = delay
        super().__init__(f"MusicBrainz HTTP {status}; retry in {delay} seconds")
