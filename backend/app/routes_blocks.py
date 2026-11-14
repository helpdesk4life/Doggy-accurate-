"""Blocking a walker.

The endpoints are thin on purpose: the block list is two Redis keys (see
``app.blocks``) and the only real work is re-evaluating the two maps that just
changed, so the effect is immediate instead of waiting for the next GPS fix.

Blocking never reports whether the id exists. A 404 for an unknown walker would
turn this into an account-existence oracle, and the client only ever blocks
someone it just saw on the map, so there is nothing to gain from being precise.
"""

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from app import auth, blocks, config, geo, proximity, users

router = APIRouter(prefix="/blocks", tags=["blocks"])

# User ids are uuid4 hex. Pinning the shape keeps a malformed id from creating a
# junk key, and rejects it before Redis is touched.
WALKER_ID = Path(pattern=r"^[0-9a-f]{32}$")


class BlockRequest(BaseModel):
    # Shown in the settings list for a walker who is not online. Optional: the
    # block is what matters, the name is only a label.
    name: str = Field(default="", max_length=64)


class BlockedWalker(BaseModel):
    id: str
    name: str


def _redis(request: Request):
    return request.app.state.redis


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


async def _require_user(request: Request) -> dict:
    """The account behind the bearer token, or 401."""
    try:
        return await users.resolve_access_token(_redis(request), _bearer(request))
    except auth.InvalidToken:
        raise HTTPException(status_code=401, detail="invalid access token")


async def _refresh(request: Request, walker_ids: set[str]) -> None:
    """Re-evaluate the given walkers so the change lands on their maps now.

    ``refresh_neighborhood`` rather than ``update_walker``: the latter also
    refreshes every neighbour of each walker, which here means each of the two
    sockets gets the same frame twice. ``last_neighbors`` is left stale on
    purpose — the next fix recomputes it, and a stale entry only costs one
    redundant refresh. It can never leak, because every refresh filters against
    the hidden set of the walker it is sent to.
    """
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        return

    redis = _redis(request)
    radius_m = config.settings.proximity_radius_m
    for walker_id in walker_ids:
        coords = await geo.position(redis, walker_id)
        if coords is None:
            continue
        await proximity.refresh_neighborhood(
            redis, hub, walker_id, coords[0], coords[1], radius_m
        )


@router.get("", response_model=list[BlockedWalker])
async def list_blocks(request: Request) -> list[BlockedWalker]:
    """Who this walker has blocked, with the names captured at block time."""
    user = await _require_user(request)
    entries = await blocks.blocked_by_me(_redis(request), user["user_id"])
    return [BlockedWalker(id=walker_id, name=name) for walker_id, name in entries]


@router.post("/{walker_id}", status_code=204)
async def block_walker(
    request: Request,
    body: BlockRequest,
    walker_id: str = WALKER_ID,
) -> Response:
    """Hide this walker and be hidden from them, both directions."""
    user = await _require_user(request)
    if walker_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="cannot block yourself")

    await blocks.block(_redis(request), user["user_id"], walker_id, body.name)
    await _refresh(request, {user["user_id"], walker_id})
    return Response(status_code=204)


@router.delete("/{walker_id}", status_code=204)
async def unblock_walker(request: Request, walker_id: str = WALKER_ID) -> Response:
    """Undo a block. Harmless when there was none."""
    user = await _require_user(request)
    await blocks.unblock(_redis(request), user["user_id"], walker_id)
    await _refresh(request, {user["user_id"], walker_id})
    return Response(status_code=204)
