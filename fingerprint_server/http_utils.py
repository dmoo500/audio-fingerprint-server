"""HTTP URL checks shared by the API and public recognition app."""

from urllib.parse import urlsplit


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
