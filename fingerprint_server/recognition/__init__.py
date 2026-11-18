"""Standalone public microphone recognition service."""

from fingerprint_server.recognition.app import app, create_app

__all__ = ["app", "create_app"]
