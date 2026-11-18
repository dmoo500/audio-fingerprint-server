"""Optional administrator service; keep the Uvicorn entry point stable."""

from fingerprint_server.admin.app import AdminSettings, app, create_admin

__all__ = ["AdminSettings", "app", "create_admin"]
