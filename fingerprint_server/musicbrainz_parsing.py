"""Pure validation of MusicBrainz recording and artwork responses."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID

from fingerprint_server.schemas import ISRC_PATTERN


def _album_kind(release: dict[str, Any]) -> bool | None:
    """Identify a non-compilation album, or return None for unknown types."""
    group = release.get("release-group")
    if not isinstance(group, dict) or not isinstance(group.get("primary-type"), str):
        return None
    if group["primary-type"] != "Album":
        return False
    secondary = group.get("secondary-types")
    if not isinstance(secondary, list) or any(not isinstance(item, str) for item in secondary):
        return None
    return "Compilation" not in secondary


def _official_album_date(release: dict[str, Any]) -> str | None:
    """Return a known release date for an official album linked to the recording."""
    if release.get("status") != "Official" or _album_kind(release) is not True:
        return None
    value = release.get("date")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){0,2}", value):
        return None
    try:
        date.fromisoformat(value + "-01" * (2 - value.count("-")))
    except ValueError:
        return None
    return value


def parse_artwork(value: dict[str, Any], *, offset: int = 0) -> dict[str, Any]:
    """Validate one recording-associated release page and its cover-selection evidence."""
    releases = value.get("releases")
    count = value.get("release-count")
    returned_offset = value.get("release-offset")
    if (
        not isinstance(releases, list)
        or len(releases) > 100
        or type(count) is not int
        or type(returned_offset) is not int
        or returned_offset != offset
        or count < offset + len(releases)
        or (not releases and count > offset)
    ):
        raise ValueError("Invalid artwork releases")
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("title"), str)
        or not isinstance(item.get("release-group"), dict)
        for item in releases
    ):
        raise ValueError("Invalid artwork release identity")
    return {
        "status": "resolved",
        "complete": offset + len(releases) == count,
        "releases": [
            {
                "title": item["title"],
                "group": str(UUID(item["release-group"]["id"])),
                **({"id": str(UUID(item["id"]))} if isinstance(item.get("id"), str) else {}),
                **(
                    {"group_title": item["release-group"]["title"]}
                    if isinstance(item["release-group"].get("title"), str)
                    else {}
                ),
                **(
                    {"group_type": item["release-group"]["primary-type"]}
                    if isinstance(item["release-group"].get("primary-type"), str)
                    else {}
                ),
                "status": item.get("status"),
                "date": item.get("date") if isinstance(item.get("date"), str) else None,
                "front": isinstance(item.get("cover-art-archive"), dict)
                and item["cover-art-archive"].get("front") is True,
            }
            for item in releases
        ],
    }


def parse_recording(value: Any) -> dict[str, Any]:
    """Extract only validated recording data from a MusicBrainz response."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ValueError("Invalid recording")
    result: dict[str, Any] = {"id": str(UUID(value["id"]))}
    disambiguation = value.get("disambiguation", "")
    if not isinstance(disambiguation, str) or len(disambiguation) > 1000:
        raise ValueError("Invalid recording disambiguation")
    result["disambiguation"] = disambiguation
    title = value.get("title")
    if title is not None:
        if not isinstance(title, str) or len(title) > 500:
            raise ValueError("Invalid recording title")
        result["title"] = title
    length = value.get("length")
    if length is not None:
        if type(length) is not int or length < 0:
            raise ValueError("Invalid recording length")
        result["length"] = length
    credits = value.get("artist-credit", [])
    if not isinstance(credits, list) or len(credits) > 30:
        raise ValueError("Invalid artists")
    artists = []
    artist_names = []
    credit_names = []
    for credit in credits:
        if not isinstance(credit, dict):
            raise ValueError("Invalid artist")
        artist = credit.get("artist") or {}
        if not isinstance(artist, dict):
            raise ValueError("Invalid artist")
        name = credit.get("name") or artist.get("name")
        if not isinstance(name, str) or len(name) > 500:
            raise ValueError("Invalid artist name")
        if name.strip():
            artists.append(name)
            artist_names.append(name)
        names = [name]
        canonical = artist.get("name")
        if isinstance(canonical, str) and canonical.strip():
            artist_names.append(canonical)
            names.append(canonical)
        aliases = artist.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 100:
            raise ValueError("Invalid artist aliases")
        for alias in aliases:
            if not isinstance(alias, dict) or not isinstance(alias.get("name"), str):
                raise ValueError("Invalid artist alias")
            if len(alias["name"]) > 500:
                raise ValueError("Invalid artist alias")
            artist_names.append(alias["name"])
            names.append(alias["name"])
        if name.strip():
            credit_names.append(names)
    result["artists"] = artists
    result["artist_names"] = artist_names
    result["credit_names"] = credit_names
    releases = value.get("releases", [])
    if not isinstance(releases, list) or len(releases) > 1000:
        raise ValueError("Invalid releases")
    albums = set()
    review_albums = set()
    review_releases: set[tuple[str, str]] = set()
    album_types_complete = True
    release_names = set()
    release_ids: list[tuple[str | None, str | None]] = []
    dated_albums: list[tuple[str, str]] = []
    for release in releases:
        if not isinstance(release, dict) or not isinstance(release.get("title"), str):
            raise ValueError("Invalid release")
        album = release["title"]
        if len(album) > 500:
            raise ValueError("Invalid release title")
        kind = _album_kind(release)
        if kind is None:
            album_types_complete = False
        group = release.get("release-group")
        if kind is None:
            category = "unknown"
        elif kind:
            category = "album"
        elif isinstance(group, dict) and group.get("primary-type") == "Single":
            category = "single"
        elif isinstance(group, dict) and group.get("primary-type") == "Album":
            category = "compilation"
        else:
            category = "other"
        if album.strip():
            release_names.add(album)
            if category != "album":
                review_releases.add((album, category))
            if kind is True:
                albums.add(album)
                review_albums.add(album)
                if release_date := _official_album_date(release):
                    dated_albums.append((release_date, album))
        if isinstance(group, dict) and isinstance(group.get("title"), str):
            release_names.add(group["title"][:500])
            if kind is True and group["title"].strip():
                review_albums.add(group["title"][:500])
            elif group["title"].strip():
                review_releases.add((group["title"][:500], category))
        release_id = release.get("id")
        group_id = group.get("id") if isinstance(group, dict) else None
        release_ids.append(
            (
                release_id.lower() if isinstance(release_id, str) else None,
                group_id.lower() if isinstance(group_id, str) else None,
            )
        )
    release_count = value.get("release-count")
    release_offset = value.get("release-offset", 0)
    result["albums"] = sorted(release_names)
    result["review_albums"] = sorted(review_albums)
    result["review_releases"] = [
        {"title": title, "type": category} for title, category in sorted(review_releases)
    ]
    result["release_ids"] = release_ids
    if dated_albums:
        release_date, title = min(dated_albums)
        result["earliest_album"] = {"title": title, "date": release_date}
    result["albums_complete"] = type(release_count) is int and (
        release_count == len(releases) and type(release_offset) is int and release_offset == 0
    )
    if (
        len(albums) == 1
        and album_types_complete
        and type(release_count) is int
        and release_count == len(releases)
        and type(release_offset) is int
        and release_offset == 0
    ):
        result["album"] = albums.pop()
    isrcs = value.get("isrcs")
    if isrcs is not None and (
        not isinstance(isrcs, list)
        or len(isrcs) > 1000
        or any(
            not isinstance(code, str) or re.fullmatch(ISRC_PATTERN, code) is None for code in isrcs
        )
    ):
        raise ValueError("Invalid ISRC list")
    result["isrcs"] = [code.upper() for code in isrcs] if isrcs is not None else None
    return result
