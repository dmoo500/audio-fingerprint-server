"""Typed application state for the fingerprint API."""

import asyncio

from aiohttp import web

from fingerprint_server.database import Storage
from fingerprint_server.database.recognition_metrics import Identity, Outcome
from fingerprint_server.settings import Settings
from fingerprint_server.worker import Worker

SETTINGS = web.AppKey[Settings]("settings")
STORE = web.AppKey[Storage]("store")
WORKER = web.AppKey[Worker | None]("worker")
WORKER_TASK = web.AppKey[asyncio.Task[None] | None]("worker_task")
OWNERSHIP_TASK = web.AppKey[asyncio.Task[None] | None]("ownership_task")
RECOGNITION_OUTCOME = web.RequestKey[Outcome]("recognition_outcome")
RECOGNITION_IDENTITY = web.RequestKey[Identity]("recognition_identity")
