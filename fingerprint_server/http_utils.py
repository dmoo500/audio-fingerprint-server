"""HTTP helpers shared by the fingerprint services."""

import math
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


def retry_after(value: str | None) -> float:
    """Return the delay requested by a Retry-After header."""
    if not value:
        return 0.0
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
        except ValueError:
            return 0.0
        return seconds if math.isfinite(seconds) else 0.0
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


def trusted_image(url: str) -> bool:
    """Accept only credential-free HTTPS images hosted by the cover archive."""
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (
            host == "archive.org" or host.endswith(".archive.org") or host == "coverartarchive.org"
        )
    )
