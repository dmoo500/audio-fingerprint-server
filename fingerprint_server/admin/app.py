"""Framework-backed administration, isolated from the public fingerprint API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from pydantic import Field, HttpUrl, SecretStr, model_validator
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette_admin import (
    BaseModelView,
    BooleanField,
    CustomView,
    IntegerField,
    JSONField,
    StringField,
    action,
    row_action,
)
from starlette_admin.auth import AdminUser, AuthProvider
from starlette_admin.base import BaseAdmin
from starlette_admin.exceptions import ActionFailed, LoginFailed

from fingerprint_server.access_log import AccessLogMiddleware
from fingerprint_server.admin.data import AdminData, Dataset, Filter
from fingerprint_server.admin.history import QUEUE, RECORDINGS, chart
from fingerprint_server.admin.live import AdminLive
from fingerprint_server.async_utils import run_sync
from fingerprint_server.database.application_keys import ApplicationKeys
from fingerprint_server.database.application_keys import validate as validate_application_keys
from fingerprint_server.database.metrics import HOUR
from fingerprint_server.settings import Settings

LOGGER = logging.getLogger(__name__)
TEMPLATES = Path(__file__).with_name("templates")


class AdminSettings(Settings):
    """Explicit configuration for the separately published administration service."""

    model_config = {**Settings.model_config, "hide_input_in_errors": True}

    admin_key: SecretStr = Field(min_length=32)
    admin_cookie_secure: bool = False
    gateway_admin_key: SecretStr = Field(min_length=24)
    gateway_admin_url: HttpUrl = HttpUrl("http://gateway:9180/apisix/admin/")

    @model_validator(mode="after")
    def validate_admin(self) -> AdminSettings:
        """Require PostgreSQL and separate credentials without URL credentials."""
        if self.database_backend != "postgres":
            raise ValueError("The admin service currently requires PostgreSQL")
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
        return self


class AdminProtection(BaseHTTPMiddleware):
    """Prevent browser cross-origin writes, GET actions and caching of administrative data."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.endswith(("/action", "/row-action")) and request.method != "POST":
            return Response("Administrative actions require POST", status_code=405)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if request.headers.get("origin") != expected:
                return Response("A same-origin administration request is required", status_code=403)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Referrer-Policy": "same-origin",
            }
        )
        return response


class AdminAuth(AuthProvider):
    """Short-lived server-side sessions; no credentials are stored in browser session data."""

    def __init__(self, key: SecretStr) -> None:
        super().__init__()
        self.key = key
        self.sessions: dict[str, float] = {}
        self.attempts: deque[float] = deque()

    async def login(
        self,
        username: str,
        password: str,
        remember_me: bool,
        request: Request,
        response: Response,
    ) -> Response:
        """Authenticate the single operator using a separate admin key."""
        now = time.monotonic()
        while self.attempts and self.attempts[0] < now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            raise LoginFailed("Too many login attempts; wait one minute")
        self.attempts.append(now)
        valid_key = isinstance(password, str) and hmac.compare_digest(
            password.encode(), self.key.get_secret_value().encode()
        )
        if username != "admin" or not valid_key:
            LOGGER.warning("Rejected administrator login")
            raise LoginFailed("Invalid administrator credentials")
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        if len(self.sessions) >= 20:
            raise LoginFailed("Too many active admin sessions")
        previous = request.session.get("token", "")
        self.sessions.pop(previous, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + 3600
        request.session.clear()
        request.session["token"] = token
        response.headers["location"] = str(request.url_for("admin:index"))
        LOGGER.info("Administrator logged in")
        return response

    async def is_authenticated(self, request: Request) -> bool:
        """Accept only a server-held unexpired session."""
        token = request.session.get("token", "")
        return self.sessions.get(token, 0) > time.monotonic()

    async def logout(self, request: Request, response: Response) -> Response:
        """Invalidate the session immediately, including copied cookies."""
        self.sessions.pop(request.session.get("token", ""), None)
        request.session.clear()
        return response

    def get_admin_user(self, request: Request) -> AdminUser:
        """Identify the authenticated operator."""
        return AdminUser(username="admin")


class DataView(BaseModelView):
    """Metadata-only framework table backed by existing SQL, not ORM models."""

    pk_attr = "id"
    list_template = "admin_list.html"
    page_size = 50
    page_size_options = [25, 50, 100]
    export_types = []
    save_state = False
    fields_default_sort = [("created_at", True)]
    datatables_options = {"scrollY": "60vh", "scrollCollapse": True, "scrollX": True}

    def __init__(self, data: AdminData, dataset: Dataset, label: str) -> None:
        self.data = data
        self.dataset = dataset
        self.identity = dataset
        self.name = label
        self.label = label
        self.fields = [
            StringField("id", label="ID"),
            StringField("title"),
            StringField("artists"),
            StringField("album"),
            StringField("isrc", label="ISRC"),
            StringField("recording_mbid", label="Recording MBID"),
            StringField("created_at", label="Created (UTC)", searchable=False),
        ]
        if dataset == "references":
            self.fields += [
                BooleanField("publication_ready", searchable=False),
                IntegerField("verification_version", searchable=False),
                StringField("recording_key"),
            ]
            self.row_actions = ["view"]
            self.actions = []
        else:
            self.fields += [
                StringField("state"),
                StringField("error"),
                IntegerField("attempts", searchable=False),
                IntegerField("pending_rechecks", searchable=False),
                StringField("reference_id"),
            ]
            self.row_actions = ["view", "retry", "remove"]
            self.actions = ["retry_selected", "remove_selected"]
        self.fields += [JSONField("metadata", exclude_from_list=True, searchable=False)]
        self.searchable_fields = list(data.fields(dataset))
        self.sortable_fields = list(data.sortable_fields(dataset))
        super().__init__()

    def can_create(self, request: Request) -> bool:
        """Never create raw database rows from the administration interface."""
        return False

    def can_edit(self, request: Request) -> bool:
        """Never bypass fingerprint verification with inline metadata editing."""
        return False

    def can_delete(self, request: Request) -> bool:
        """Use only the guarded explicit remove action."""
        return False

    async def is_action_allowed(self, request: Request, name: str) -> bool:
        """Enforce the action allowlist on requests, not only in the menu."""
        return name in (self.actions or ()) and await super().is_action_allowed(request, name)

    async def is_row_action_allowed(self, request: Request, name: str) -> bool:
        """Keep reference views read-only even for manually constructed requests."""
        return name in (self.row_actions or ()) and await super().is_row_action_allowed(
            request, name
        )

    async def create(self, request: Request, data: dict[str, Any]) -> Any:
        """Reject raw database inserts."""
        raise ActionFailed("Raw inserts are disabled")

    async def edit(self, request: Request, pk: Any, data: dict[str, Any]) -> Any:
        """Reject arbitrary metadata updates."""
        raise ActionFailed("Raw editing is disabled")

    async def delete(self, request: Request, pks: list[Any]) -> int | None:
        """Reject the unguarded framework delete operation."""
        raise ActionFailed("Use Delete submission instead")

    def objects(self, rows: list[dict[str, Any]]) -> list[SimpleNamespace]:
        """Convert projected rows to framework-compatible objects."""
        from datetime import UTC, datetime

        return [
            SimpleNamespace(
                **{
                    **row,
                    "created_at": datetime.fromtimestamp(row["created_at"], UTC).isoformat(),
                    "publication_ready": bool(row.get("publication_ready")),
                }
            )
            for row in rows
        ]

    async def find_all(
        self,
        request: Request,
        skip: int = 0,
        limit: int = 100,
        where: Filter = None,
        order_by: list[str] | None = None,
    ) -> list[SimpleNamespace]:
        """Read one bounded server-filtered table page."""
        result = await run_sync(
            self.data.read, self.dataset, where, skip=skip, limit=limit, order_by=order_by
        )
        assert isinstance(result, list)
        return self.objects(result)

    async def count(self, request: Request, where: Filter = None) -> int:
        """Count entries matching the current filter."""
        result = await run_sync(self.data.read, self.dataset, where, count=True)
        assert isinstance(result, int)
        return result

    async def find_by_pks(self, request: Request, pks: list[Any]) -> list[SimpleNamespace]:
        """Read selected metadata without exposing encoded landmarks."""
        result = await run_sync(self.data.read, self.dataset, pks=pks, limit=100)
        assert isinstance(result, list)
        return self.objects(result)

    async def find_by_pk(self, request: Request, pk: Any) -> SimpleNamespace | None:
        """Read a single detail view."""
        rows = await self.find_by_pks(request, [pk])
        return rows[0] if rows else None

    @row_action(
        "retry",
        "Retry",
        confirmation="Retry this inactive submission?",
        icon_class="fa-solid fa-rotate-right",
        form="<form></form>",
    )
    async def retry(self, request: Request, pk: str) -> str:
        """Queue a failed or unresolved submission using guarded admission."""
        await run_sync(self.data.mutate, [pk], "retry")
        return "Queued; attempt count reset, pending check count preserved"

    @row_action(
        "remove",
        "Delete submission",
        confirmation="Delete this submission? Its acoustic reference will be retained.",
        icon_class="fa-solid fa-trash",
        submit_btn_class="btn-danger",
        form="<form></form>",
    )
    async def remove(self, request: Request, pk: str) -> str:
        """Delete a durable inactive submission without deleting acoustic data."""
        await run_sync(self.data.mutate, [pk], "delete")
        return "Submission deleted; acoustic reference retained"

    @action(
        "retry_selected",
        "Retry selected",
        confirmation="Retry the selected inactive submissions?",
        form="<form></form>",
    )
    async def retry_selected(self, request: Request, pks: list[str]) -> str:
        """Queue a bounded selection atomically."""
        await run_sync(self.data.mutate, pks, "retry")
        return "Selected submissions queued"

    @action(
        "remove_selected",
        "Delete selected submissions",
        confirmation="Delete selected submissions? Acoustic references will be retained.",
        submit_btn_class="btn-danger",
        form="<form></form>",
    )
    async def remove_selected(self, request: Request, pks: list[str]) -> str:
        """Delete a bounded inactive selection atomically."""
        await run_sync(self.data.mutate, pks, "delete")
        return "Selected submissions deleted; acoustic references retained"


class Dashboard(CustomView):
    """Operational overview and explicit non-destructive pending review."""

    def __init__(self, data: AdminData, live: AdminLive) -> None:
        super().__init__(
            "Overview", path="/", template_path="admin_overview.html", methods=["GET", "POST"]
        )
        self.data = data
        self.live = live

    async def render(self, request: Request, templates: Jinja2Templates) -> Response:
        """Render the overview without periodically scanning landmark tables."""
        message = None
        if request.method == "POST":
            await run_sync(self.data.store.request_pending_recheck)
            LOGGER.info("Admin requested non-destructive pending review")
            return RedirectResponse(str(request.url.replace(query="review=requested")), 303)
        if request.query_params.get("review") == "requested":
            message = "Pending review requested. Unresolved entries are retained by this pass."
        hour = int(time.time()) // HOUR
        samples = await run_sync(self.data.hourly_history, hour)
        charts = (
            ("Searchable recordings", chart(samples, hour, RECORDINGS)),
            ("Submission backlog", chart(samples, hour, QUEUE)),
        )
        if request.query_params.get("view") == "history":
            return templates.TemplateResponse(
                request=request,
                name="admin_history.html",
                context={"charts": charts},
                headers={"Cache-Control": "no-store"},
            )
        return templates.TemplateResponse(
            request=request,
            name=self.template_path,
            context={
                "title": "Overview",
                "overview": self.live.snapshot["overview"],
                "queue_series": QUEUE,
                "charts": charts,
                "message": message,
                "live_failed": self.live.failed,
            },
        )


class MusicBrainzReviews(CustomView):
    """Review pending MusicBrainz candidates without contacting MusicBrainz."""

    def __init__(self, data: AdminData) -> None:
        super().__init__(
            "MusicBrainz reviews",
            name="musicbrainz-reviews",
            path="/musicbrainz-reviews",
            template_path="admin_musicbrainz_reviews.html",
            methods=["GET", "POST"],
            icon="fa-solid fa-music",
        )
        self.data = data

    async def render(self, request: Request, templates: Jinja2Templates) -> Response:
        """List pending candidates or record a validated selection."""
        error = None
        page_value: Any = request.query_params.get("skip", "0")
        if request.method == "POST":
            form = await request.form()
            page_value = form.get("skip", page_value)
            submission_id = form.get("submission_id")
            selected_mbid = form.get("selected_mbid")
            if not isinstance(submission_id, str) or not isinstance(selected_mbid, str):
                error = "Select one of the listed MusicBrainz candidates"
            else:
                try:
                    await run_sync(
                        self.data.select_musicbrainz_candidate, submission_id, selected_mbid
                    )
                except ActionFailed as err:
                    error = err.msg
                else:
                    return RedirectResponse(
                        str(request.url.replace(query="selected=1")), status_code=303
                    )
        try:
            skip = int(page_value)
        except (TypeError, ValueError):
            skip = -1
        try:
            items = await run_sync(self.data.musicbrainz_reviews, skip)
        except ActionFailed as err:
            items = []
            error = err.msg
            skip = 0
        has_more = len(items) > 50
        items = items[:50]
        return templates.TemplateResponse(
            request=request,
            name=self.template_path,
            context={
                "title": "MusicBrainz reviews",
                "items": items,
                "error": error,
                "skip": skip,
                "has_previous": skip > 0,
                "has_more": has_more,
                "message": (
                    "Candidate selected and submission queued."
                    if request.query_params.get("selected") == "1"
                    else None
                ),
            },
            status_code=400 if error else 200,
        )


class Keys(CustomView):
    """Manage personal APISIX consumers without disclosing stored credentials."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(
            "API keys",
            name="keys",
            path="/keys",
            template_path="admin_keys.html",
            methods=["GET", "POST"],
            icon="fa-solid fa-key",
        )
        self.client = client

    async def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Call the private gateway API and redact upstream error details."""
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as err:
            LOGGER.error("Admin gateway operation failed: %s", type(err).__name__)
            raise ActionFailed("Gateway administration failed; see server log") from err

    async def render(self, request: Request, templates: Jinja2Templates) -> Response:
        """List key identities; new key values exist only in the creation response."""
        error = None
        new_key = None
        try:
            if request.method == "POST":
                form = await request.form()
                username = str(form.get("username", ""))
                operation = form.get("operation")
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
                    raise ActionFailed("Use 1 to 64 letters, digits, underscores or hyphens")
                if operation == "issue":
                    role = str(form.get("role"))
                    if role not in ("read", "upload", "both"):
                        raise ActionFailed("Select read, upload or both")
                    if len(username) > 31:
                        raise ActionFailed("New key name prefixes are limited to 31 characters")
                    username += "-" + secrets.token_hex(16)
                    key = secrets.token_urlsafe(32)
                    # APISIX only offers upsert; random identities avoid replacing named consumers.
                    await self.call(
                        "PUT",
                        f"consumers/{username}",
                        json={
                            "username": username,
                            "group_id": role,
                            "plugins": {"key-auth": {"key": key}},
                        },
                    )
                    new_key = key
                    LOGGER.info("Admin issued personal key user=%s role=%s", username, role)
                elif operation == "revoke":
                    if form.get("confirmed") != "yes":
                        raise ActionFailed("Confirm revocation first")
                    existing = (await self.call("GET", f"consumers/{username}")).json()["value"]
                    if existing.get("group_id") not in ("read", "upload", "both"):
                        raise ActionFailed("This is not a fingerprint personal consumer")
                    await self.call("DELETE", f"consumers/{username}")
                    LOGGER.info("Admin revoked personal key user=%s", username)
                    return RedirectResponse(str(request.url.replace(query="")), 303)
                else:
                    raise ActionFailed("Unknown API-key operation")
            document = (await self.call("GET", "consumers")).json()
            items = [
                {"username": item["value"]["username"], "role": item["value"]["group_id"]}
                for item in document.get("list", [])
                if item["value"].get("group_id") in ("read", "upload", "both")
            ]
        except ActionFailed as err:
            error = err.msg
            items = []
        return templates.TemplateResponse(
            request=request,
            name=self.template_path,
            context={
                "title": "API keys",
                "items": sorted(items, key=lambda item: item["username"]),
                "error": error,
                "new_key": new_key,
            },
            status_code=400 if error else 200,
        )


class Applications(CustomView):
    """Manage separate application credentials without exposing stored secrets."""

    def __init__(self, data: AdminData, config: AdminSettings) -> None:
        super().__init__(
            "Application keys",
            name="applications",
            path="/applications",
            template_path="admin_applications.html",
            methods=["GET", "POST"],
            icon="fa-solid fa-cubes",
        )
        self.registry = ApplicationKeys(data.store)
        self.legacy_configured = config.gateway_enabled and config.application_key is not None

    async def render(self, request: Request, templates: Jinja2Templates) -> Response:
        """List application identities and explicitly issue or revoke their keys."""
        error = None
        new_key = None
        try:
            if request.method == "POST":
                form = await request.form()
                operation = form.get("operation")
                if operation == "issue":
                    identifier, new_key = await run_sync(
                        self.registry.issue, str(form.get("name", ""))
                    )
                    LOGGER.info("Admin issued application key id=%s", identifier)
                elif operation == "revoke":
                    if form.get("confirmed") != "yes":
                        raise ValueError("Confirm revocation first")
                    identifier = str(form.get("id", ""))
                    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                        raise ValueError("Invalid application identity")
                    await run_sync(self.registry.revoke, identifier)
                    LOGGER.info("Admin revoked application key id=%s", identifier)
                    return RedirectResponse(str(request.url.replace(query="")), 303)
                else:
                    raise ValueError("Unknown application-key operation")
        except ValueError as err:
            error = str(err)
        return templates.TemplateResponse(
            request=request,
            name=self.template_path,
            context={
                "title": "Application keys",
                "items": await run_sync(self.registry.list),
                "new_key": new_key,
                "error": error,
                "legacy_configured": self.legacy_configured,
            },
            status_code=400 if error else 200,
        )


class FingerprintAdmin(BaseAdmin):
    """Return explicit validation errors for bounded metadata table requests."""

    async def _render_api(self, request: Request) -> Response:
        """Validate pagination before the framework parses and runs the query."""
        try:
            if len(request.query_params.get("where", "")) > 5000:
                raise ActionFailed("Filter expression is too large")
            return await super()._render_api(request)
        except (ValueError, ActionFailed) as err:
            message = err.msg if isinstance(err, ActionFailed) else "Invalid table request"
            return JSONResponse({"detail": message}, status_code=422)


def create_admin(configuration: AdminSettings | None = None) -> FastAPI:
    """Create an independent service without starting workers or migrating data."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = configuration or AdminSettings()  # type: ignore[call-arg]
        data = AdminData(config)
        # Fail startup if the database is missing; never create or migrate a live schema here.
        live = AdminLive(data)

        def check_registry() -> None:
            with data.store.connect() as db:
                validate_application_keys(db)

        await run_sync(check_registry)
        await live.sample()
        if live.failed:
            raise RuntimeError("Cannot initialize admin live data; see server log")
        auth = AdminAuth(config.admin_key)

        async def events(request: Request) -> Response:
            async def stream() -> AsyncIterator[str]:
                live.subscribers += 1
                try:
                    while True:
                        if not await auth.is_authenticated(request):
                            yield "event: session-expired\ndata: {}\n\n"
                            return
                        changed = live.changed
                        event = "unavailable" if live.failed else "snapshot"
                        payload = {} if live.failed else live.snapshot
                        yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                        try:
                            await asyncio.wait_for(changed.wait(), timeout=10)
                        except TimeoutError:
                            continue
                finally:
                    live.subscribers -= 1

            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )

        async with httpx.AsyncClient(
            base_url=str(config.gateway_admin_url).rstrip("/") + "/",
            headers={"X-API-KEY": config.gateway_admin_key.get_secret_value()},
            trust_env=False,
            follow_redirects=False,
            timeout=15,
        ) as gateway:
            admin = FingerprintAdmin(
                title="Fingerprint administration",
                templates_dir=str(TEMPLATES),
                index_view=Dashboard(data, live),
                auth_provider=auth,
                middlewares=[
                    Middleware(
                        SessionMiddleware,
                        secret_key=secrets.token_urlsafe(32),
                        session_cookie="fp_admin",
                        max_age=3600,
                        same_site="strict",
                        https_only=config.admin_cookie_secure,
                    )
                ],
            )
            admin.add_view(DataView(data, "submissions", "Submissions"))
            admin.add_view(DataView(data, "unresolved", "Unresolved ISRC submissions"))
            admin.add_view(DataView(data, "references", "References"))
            admin.add_view(MusicBrainzReviews(data))
            admin.add_view(Keys(gateway))
            admin.add_view(Applications(data, config))
            admin.routes.append(Route("/events", events, methods=["GET"], name="events"))
            admin.templates.env.globals["admin_live_revision"] = lambda identity: live.snapshot[
                "revisions"
            ][identity]
            admin.mount_to(application)
            application.state.admin = admin
            application.state.data = data
            application.state.live = live
            task = asyncio.create_task(live.run())
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/")

    app.add_middleware(AdminProtection)
    app.add_middleware(AccessLogMiddleware)
    return app


app = create_admin()
