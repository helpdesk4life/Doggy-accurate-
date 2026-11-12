import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app import auth, config, geo, users
from app.hub import Connection, Hub
from app.protocol import ErrorFrame, LocationFrame, Welcome
from app.proximity import refresh_watchers, update_walker

logger = logging.getLogger(__name__)

router = APIRouter()

# Application close code for "your token is missing, expired or revoked".
# The client treats this as "refresh and reconnect", not "network is down".
UNAUTHORIZED = 4401


def _bearer(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _clean_name(raw: str) -> str:
    """Strip control characters and collapse whitespace.

    The name is echoed to every walker in range, so it must not be able to carry
    a newline into a log line or a terminal escape into someone else's client.
    """
    printable = "".join(ch for ch in raw if ch.isprintable())
    return " ".join(printable.split())[:64]


@router.websocket("/ws/walk")
async def walk_socket(
    websocket: WebSocket,
    name: str = Query("", max_length=64),
) -> None:
    await websocket.accept()

    redis = websocket.app.state.redis
    hub: Hub = websocket.app.state.hub
    radius_m = config.settings.proximity_radius_m

    # The token travels in the Authorization header, not the query string, so it
    # never lands in the access log.
    token = _bearer(websocket)
    if token is None:
        await websocket.close(code=UNAUTHORIZED, reason="missing bearer token")
        return

    try:
        user = await users.resolve_access_token(redis, token)
    except auth.InvalidToken as exc:
        logger.info("rejected walk socket: %s", exc)
        await websocket.close(code=UNAUTHORIZED, reason="invalid or expired token")
        return

    # The walker id is the account id. A client cannot claim to be someone else.
    walker_id = user["user_id"]
    name = _clean_name(name)

    hub.add(Connection(walker_id=walker_id, name=name, websocket=websocket))
    await hub.send(
        walker_id,
        Welcome(walker_id=walker_id, radius_m=radius_m).model_dump(),
    )
    logger.info("walker %s connected (%s)", walker_id, name or "unnamed")

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except json.JSONDecodeError:
                await hub.send(walker_id, ErrorFrame(message="malformed JSON").model_dump())
                continue

            try:
                frame = LocationFrame.model_validate(raw)
            except ValidationError as exc:
                await hub.send(
                    walker_id,
                    ErrorFrame(message=exc.errors()[0]["msg"]).model_dump(),
                )
                continue

            await geo.upsert(redis, walker_id, frame.lat, frame.lon)
            await update_walker(redis, hub, walker_id, frame.lat, frame.lon, radius_m)

    except WebSocketDisconnect:
        pass
    finally:
        connection = hub.get(walker_id)
        watchers = set(connection.last_neighbors) if connection else set()
        hub.remove(walker_id)
        await geo.remove(redis, walker_id)
        await refresh_watchers(redis, hub, watchers, radius_m)
        logger.info("walker %s disconnected", walker_id)
