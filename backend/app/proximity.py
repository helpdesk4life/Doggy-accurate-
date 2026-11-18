"""Proximity evaluation shared by the WebSocket handler and the expiry sweeper."""

from app import blocks, config, geo, privacy
from app.hub import Hub
from app.protocol import NearbyWalker, ProximityAlert, ProximityClear


async def refresh_neighborhood(
    redis,
    hub: Hub,
    walker_id: str,
    lat: float,
    lon: float,
    radius_m: float,
    geo_redis=None,
) -> list[geo.GeoHit]:
    """Recompute one walker's neighbours and push the result to that walker.

    Sends PROXIMITY_ALERT with the full neighbour list when anyone is inside the
    radius, and PROXIMITY_CLEAR when the list is empty. Sending the whole list (rather
    than deltas) keeps the client's marker set correct without client-side bookkeeping.

    Each neighbour is described relative to ``walker_id``: a quantised distance and
    bearing, never a coordinate. The exact positions stay in Redis.

    Blocked walkers are dropped here, before the frame is built, so a block is
    enforced by the server rather than by the client choosing not to draw a
    marker. The filter is symmetric (see ``app.blocks``), which means a walker
    who blocked someone also stops appearing on that person's map.
    """
    # Blocks live on the persistent store, positions on the geo one; the two are
    # the same client unless GEO_REDIS_URL splits them.
    geo_redis = geo_redis if geo_redis is not None else redis
    hits = await geo.nearby(geo_redis, lat, lon, radius_m, exclude_id=walker_id)

    hidden = await blocks.hidden_for(redis, walker_id)
    if hidden:
        hits = [hit for hit in hits if hit.walker_id not in hidden]

    # Record what this walker was last shown, so the sweeper can tell which
    # connections an expired walker was visible to.
    connection = hub.get(walker_id)
    if connection is not None:
        connection.last_neighbors = {hit.walker_id for hit in hits}

    if hits:
        alert = ProximityAlert(
            radius_m=radius_m,
            walkers=[
                NearbyWalker(
                    id=hit.walker_id,
                    name=hub.name_of(hit.walker_id),
                    distance_m=privacy.quantise_distance(
                        hit.distance_m, config.settings.distance_quantum_m
                    ),
                    bearing_deg=privacy.quantise_bearing(
                        privacy.bearing_degrees(lat, lon, hit.lat, hit.lon),
                        config.settings.bearing_quantum_deg,
                    ),
                )
                for hit in hits
            ],
        )
        await hub.send(walker_id, alert.model_dump())
    else:
        await hub.send(walker_id, ProximityClear().model_dump())

    return hits


async def update_walker(
    redis,
    hub: Hub,
    walker_id: str,
    lat: float,
    lon: float,
    radius_m: float,
    geo_redis=None,
) -> list[geo.GeoHit]:
    """Apply a new fix and refresh everyone whose view of the world changed.

    That is the walker themselves, everyone they can now see, and everyone who could
    see them before the move. Without the last group a walker who walks out of range
    would linger on other people's maps until their coordinates expired.
    """
    connection = hub.get(walker_id)
    previous = set(connection.last_neighbors) if connection else set()

    hits = await refresh_neighborhood(
        redis, hub, walker_id, lat, lon, radius_m, geo_redis=geo_redis
    )
    current = {hit.walker_id for hit in hits}

    # One pipelined read for all neighbors' coordinates instead of one HGET each.
    others = (previous | current) - {walker_id}
    geo_redis = geo_redis if geo_redis is not None else redis
    coords_by_id = await geo.positions(geo_redis, others)
    for other_id in others:
        coords = coords_by_id.get(other_id)
        if coords is None:
            continue
        await refresh_neighborhood(
            redis, hub, other_id, coords[0], coords[1], radius_m, geo_redis=geo_redis
        )

    return hits


async def refresh_watchers(
    redis, hub: Hub, watcher_ids: set[str], radius_m: float, geo_redis=None
) -> None:
    """Re-evaluate the given walkers, e.g. after someone they were watching disappeared."""
    geo_redis = geo_redis if geo_redis is not None else redis
    coords_by_id = await geo.positions(geo_redis, watcher_ids)
    for other_id in watcher_ids:
        coords = coords_by_id.get(other_id)
        if coords is None:
            continue
        await refresh_neighborhood(
            redis, hub, other_id, coords[0], coords[1], radius_m, geo_redis=geo_redis
        )
