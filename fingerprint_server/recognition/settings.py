"""Configuration for the public recognition service."""

from pathlib import Path

import httpx
from aiohttp import web
from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RecognitionSettings(BaseSettings):
    """Configure the public origin and a dedicated, read-only upstream identity."""

    model_config = SettingsConfigDict(env_prefix="FP_WEB_", env_file=None)

    public_url: HttpUrl = HttpUrl("http://127.0.0.1:8100")
    api_url: HttpUrl = HttpUrl("http://127.0.0.1:8097")
    read_key: SecretStr = Field(min_length=24)
    application_key: SecretStr | None = Field(default=None, min_length=24)
    requests_per_minute: int = Field(default=4, ge=1, le=30)
    history_database: Path | None = None

    @field_validator("public_url", "api_url")
    @classmethod
    def plain_origin(cls, value: HttpUrl) -> HttpUrl:
        """Accept only HTTP(S) origins without credentials or URL suffixes."""
        if (
            value.username
            or value.password
            or value.query is not None
            or value.fragment is not None
            or value.path not in (None, "/")
        ):
            raise ValueError("Use an HTTP(S) origin without credentials, path, query or fragment")
        return value


SETTINGS_KEY = web.AppKey("recognition_settings", RecognitionSettings)
CLIENT_KEY = web.AppKey("recognition_client", httpx.AsyncClient)
