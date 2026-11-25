"""Environment configuration with separate read and contribution credentials."""

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MUSICBRAINZ_CACHE_TTL_DAYS = 7


class Settings(BaseSettings):
    """Settings for SQLite or explicitly selected PostgreSQL service roles."""

    model_config = SettingsConfigDict(env_prefix="FP_", env_file=None, hide_input_in_errors=True)

    database: Path = Path("data/fingerprints.db")
    database_backend: Literal["sqlite", "postgres"] = "sqlite"
    postgres_dsn: SecretStr | None = None
    postgres_schema: str = Field(default="fingerprints", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    service_role: Literal["all", "api", "worker"] = "all"
    api_replicas_enabled: bool = False
    gateway_backends: tuple[str, ...] = ("fingerprints:8097",)
    upload_key: SecretStr = Field(min_length=24)
    read_key: SecretStr = Field(min_length=24)
    gateway_enabled: bool = False
    application_key: SecretStr | None = Field(default=None, min_length=24)
    queue_limit: int = Field(default=1000, ge=1, le=100000)
    queue_bytes_limit: int = Field(default=256_000_000, ge=36_000_000)
    musicbrainz_enabled: bool = False
    musicbrainz_contact: str = ""
    musicbrainz_base_url: HttpUrl = HttpUrl("https://musicbrainz.org/ws/2")
    theaudiodb_api_key: SecretStr | None = None
    housekeeping_enabled: bool = True
    background_recheck_queue_window: int = Field(default=2, ge=1, le=1000)
    pending_max_rechecks: int = Field(default=3, ge=1, le=1000)
    pending_recheck_days: int = Field(
        default=MUSICBRAINZ_CACHE_TTL_DAYS, ge=MUSICBRAINZ_CACHE_TTL_DAYS
    )
    minimum_matches: int = Field(default=30, ge=10)
    minimum_fraction: float = Field(default=0.02, gt=0, le=1)

    @field_validator("theaudiodb_api_key", mode="before")
    @classmethod
    def empty_theaudiodb_key(cls, value: object) -> object:
        """Treat an empty optional API key as disabled."""
        return None if value == "" else value

    @field_validator("theaudiodb_api_key")
    @classmethod
    def validate_theaudiodb_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Accept only a numeric TheAudioDB v1 API key."""
        if value is not None and not re.fullmatch(r"[0-9]{1,64}", value.get_secret_value()):
            raise ValueError("TheAudioDB v1 API key must be numeric")
        return value

    @field_validator("gateway_backends")
    @classmethod
    def validate_gateway_backends(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Accept explicit host/port nodes without credentials or URL components."""
        if not value or len(set(value)) != len(value):
            raise ValueError("Gateway needs at least one unique backend")
        for node in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*:[0-9]{1,5}", node):
                raise ValueError("Gateway backends must be DNS/IPv4 host:port entries")
            if not 1 <= int(node.rsplit(":", 1)[1]) <= 65535:
                raise ValueError("Gateway backend port must be between 1 and 65535")
        return value

    @field_validator("musicbrainz_base_url")
    @classmethod
    def validate_musicbrainz_url(cls, value: HttpUrl) -> HttpUrl:
        """Require a plain HTTP(S) API base URL without embedded credentials."""
        if (
            value.username
            or value.password
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "MusicBrainz base URL must not contain credentials, a query or a fragment"
            )
        return value

    @model_validator(mode="after")
    def validate_credentials(self) -> "Settings":
        """Require separate credentials and a contact for optional online lookups."""
        if self.upload_key == self.read_key:
            raise ValueError("Use different upload and recognition keys")
        if self.gateway_enabled and self.application_key is None:
            raise ValueError("Gateway mode requires FP_APPLICATION_KEY")
        if not self.gateway_enabled and self.application_key is not None:
            raise ValueError("FP_APPLICATION_KEY requires FP_GATEWAY_ENABLED=true")
        if self.application_key in (self.upload_key, self.read_key):
            raise ValueError("The application key must differ from the internal scope keys")
        if self.musicbrainz_enabled and not self.musicbrainz_contact.strip():
            raise ValueError("MusicBrainz lookups require FP_MUSICBRAINZ_CONTACT")
        if self.theaudiodb_api_key is not None and not self.musicbrainz_enabled:
            raise ValueError("TheAudioDB lookups require MusicBrainz verification")
        if self.database_backend == "sqlite" and (
            self.postgres_dsn is not None or self.service_role != "all"
        ):
            raise ValueError(
                "PostgreSQL credentials and split roles require FP_DATABASE_BACKEND=postgres"
            )
        if self.api_replicas_enabled and (
            self.database_backend != "postgres" or self.service_role != "api"
        ):
            raise ValueError("API replicas require PostgreSQL and a separate API role")
        return self
