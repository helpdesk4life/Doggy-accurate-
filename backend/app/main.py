import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request

from app import config, email, geo, ratelimit
from app.hub import Hub
from app.proximity import refresh_neighborhood
from app.admin_ui import router as admin_ui_router
from app.routes_admin import router as admin_router
from app.routes_auth import router as auth_router
from app.routes_blocks import router as blocks_router
from app.ws import router as ws_router

logger = logging.getLogger(__name__)

# Uvicorn only configures its own loggers, so without this every `logger.info` in
# this package is dropped by the root logger's last-resort handler. That hides the
# walker connect/disconnect and rejected-socket lines, which are the first thing
# you want when a client cannot stay connected.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:     %(name)s - %(message)s",
)


async def sweep_once(app: FastAPI) -> list[str]:
    """Expire stale walkers, then refresh everyone still connected.

    Returns the ids that were expired. Exposed separately from the loop so tests can
    drive expiry deterministically instead of sleeping.
    """
    redis = app.state.redis
    hub: Hub = app.state.hub

    stale = await geo.stale_walkers(redis, config.settings.walker_ttl_seconds)
    for walker_id in stale:
        await geo.remove(redis, walker_id)

    for connection in hub.all():
        coords = await geo.position(redis, connection.walker_id)
        if coords is None:
            continue
        await refresh_neighborhood(
            redis,
            hub,
            connection.walker_id,
            coords[0],
            coords[1],
            config.settings.proximity_radius_m,
        )

    return stale


async def sweep_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(config.settings.sweep_interval_seconds)
        try:
            expired = await sweep_once(app)
            if expired:
                logger.info("expired %d stale walker(s): %s", len(expired), ", ".join(expired))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep iteration failed")


def create_app(
    redis_client=None,
    rate_limit: bool | None = None,
    invite_required: bool | None = None,
) -> FastAPI:
    # Fails the boot rather than logging a warning when the signing key is the
    # one that ships in this repository. See Settings.validate().
    config.settings.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.redis = redis_client or aioredis.from_url(
            config.settings.redis_url,
            decode_responses=True,
        )
        app.state.hub = Hub()
        # Reads the admin panel's saved configuration, so a provider set up there
        # survives a restart. Falls back to EMAIL_SENDER when nothing is saved.
        app.state.email_sender = await email.build_runtime_sender(
            app.state.redis, config.settings
        )
        task = asyncio.create_task(sweep_loop(app))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            closer = getattr(app.state.redis, "aclose", None) or getattr(
                app.state.redis, "close", None
            )
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()

    app = FastAPI(title="DogWalk proximity backend", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(admin_ui_router)
    app.include_router(blocks_router)
    app.include_router(ws_router)

    if rate_limit is None:
        rate_limit = config.settings.rate_limit_enabled
    # The http middleware only covers http scope, so the websocket handler reads
    # this flag and runs its own bucket in app/ws.py. Stored on app.state rather
    # than read from settings so a test can turn the limiter off for both.
    app.state.rate_limit = rate_limit
    if rate_limit:
        ratelimit.install(app)

    # Read by /auth/register. On app.state rather than straight from settings so
    # a test can exercise both the invite-gated and the open path.
    app.state.invite_required = (
        config.settings.invite_required if invite_required is None else invite_required
    )

    # Added last, so it is the outermost middleware and also covers the 429s the
    # limiter returns.
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Auth responses carry tokens; nothing between here and the client should
        # keep a copy. The admin API carries account data and invite codes, so it
        # gets the same treatment.
        if request.url.path.startswith(("/auth", "/admin")):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/admin/ui"):
            # The panel is server-rendered with no JavaScript at all, so the
            # policy can be strict: no scripts, no frames, forms only to self.
            # style-src needs 'unsafe-inline' for the templates' style attributes.
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self' 'unsafe-inline'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            )
            response.headers["X-Frame-Options"] = "DENY"
        return response

    if config.settings.jwt_secret_is_default:
        logger.warning(
            "JWT_SECRET is unset; using the built-in development secret. "
            "Anyone can mint tokens. This is only allowed because DOGWALK_ENV is "
            "%r; set JWT_SECRET before exposing this.",
            config.settings.environment,
        )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "radius_m": config.settings.proximity_radius_m,
            "walker_ttl_seconds": config.settings.walker_ttl_seconds,
        }

    return app


app = create_app()
