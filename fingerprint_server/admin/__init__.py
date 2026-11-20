"""Optional administrator service; keep the service import stable."""

from fingerprint_server.admin.app import AdminSettings, app, create_admin

__all__ = ["AdminSettings", "app", "create_admin"]
