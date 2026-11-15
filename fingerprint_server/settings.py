"""Environment configuration with separate read and contribution credentials."""

from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the local, single-process fingerprint service."""

    model_config = SettingsConfigDict(env_prefix="FP_", env_file=None)

    database: Path = Path("data/fingerprints.db")
    upload_key: SecretStr = Field(min_length=24)
    read_key: SecretStr = Field(min_length=24)
    gateway_enabled: bool = False
    application_key: SecretStr | None = Field(default=None, min_length=24)
    queue_limit: int = Field(default=1000, ge=1, le=100000)
    queue_bytes_limit: int = Field(default=256_000_000, ge=36_000_000)
    musicbrainz_enabled: bool = False
    musicbrainz_contact: str = ""
    musicbrainz_base_url: HttpUrl = HttpUrl("https://musicbrainz.org/ws/2")
    minimum_matches: int = Field(default=30, ge=10)
    minimum_fraction: float = Field(default=0.02, gt=0, le=1)

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
        return self
