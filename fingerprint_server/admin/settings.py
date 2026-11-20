"""Settings for the private administrator service."""

from pydantic import Field, HttpUrl, SecretStr, model_validator

from fingerprint_server.settings import Settings


class AdminSettings(Settings):
    """Configure the separately published administration service."""

    model_config = {**Settings.model_config, "hide_input_in_errors": True}

    admin_key: SecretStr = Field(min_length=32)
    admin_cookie_secure: bool = False
    gateway_admin_key: SecretStr = Field(min_length=24)
    gateway_admin_url: HttpUrl = HttpUrl("http://gateway:9180/apisix/admin/")
    worker_health_url: HttpUrl = HttpUrl("http://fingerprints:8097/health")

    @model_validator(mode="after")
    def validate_admin(self) -> "AdminSettings":
        """Require separate credentials and URLs without embedded credentials."""
        if self.postgres_dsn is not None and not self.postgres_dsn.get_secret_value():
            raise ValueError("The admin PostgreSQL DSN must not be empty")
        if self.admin_key in (
            self.upload_key,
            self.read_key,
            self.application_key,
            self.gateway_admin_key,
        ):
            raise ValueError("Use a separate admin UI key")
        url = self.gateway_admin_url
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Gateway admin URL must not include credentials/query/fragment")
        url = self.worker_health_url
        if url.username or url.password or url.query or url.fragment or url.path != "/health":
            raise ValueError("Worker health URL must be a plain /health URL")
        return self
