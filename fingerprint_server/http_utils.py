"""HTTP URL checks shared by the API and public recognition app."""

from urllib.parse import urlsplit


def trusted_image(url: str) -> bool:
    """Accept only credential-free HTTPS images hosted by the cover archive."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and (
            host == "archive.org" or host.endswith(".archive.org") or host == "coverartarchive.org"
        )
    )
