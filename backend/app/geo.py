"""Redis GEO operations for the walker set.

Two keys are kept in step:

* ``GEO_KEY``  - a sorted set of walker ids scored by geohash, used for GEOSEARCH.
* ``POS_KEY``  - walker id -> "lat,lon", the exact coordinates the client reported.
* ``SEEN_KEY`` - sorted set of walker id scored by last-fix timestamp, so the
                 sweeper finds stale entries with one ZRANGEBYSCORE instead of
                 reading the whole key.

Redis has no per-member TTL inside a sorted set, so expiry is enforced by the sweeper
in ``main.py``: members whose score is older than ``WALKER_TTL_SECONDS`` are removed.
"""

import time
from dataclasses import dataclass

GEO_KEY = "dogwalk:walkers:geo"
POS_KEY = "dogwalk:walkers:pos"
SEEN_KEY = "dogwalk:walkers:seen"


@dataclass(frozen=True)
class GeoHit:
    walker_id: str
    lat: float
    lon: float
    distance_m: float


async def upsert(redis, walker_id: str, lat: float, lon: float, now: float | None = None) -> None:
    """Record a fix: GEOADD for the spatial index, plus position and last-seen."""
    now = time.time() if now is None else now
    async with redis.pipeline(transaction=False) as pipe:
        pipe.geoadd(GEO_KEY, (lon, lat, walker_id))
        pipe.hset(POS_KEY, walker_id, f"{lat},{lon}")
        pipe.zadd(SEEN_KEY, {walker_id: now})
        await pipe.execute()


async def remove(redis, walker_id: str) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zrem(GEO_KEY, walker_id)
        pipe.hdel(POS_KEY, walker_id)
        pipe.zrem(SEEN_KEY, walker_id)
        await pipe.execute()


async def remove_many(redis, walker_ids) -> None:
    """Remove several walkers in one round trip instead of one pipeline each."""
    walker_ids = list(walker_ids)
    if not walker_ids:
        return
    async with redis.pipeline(transaction=False) as pipe:
        for walker_id in walker_ids:
            pipe.zrem(GEO_KEY, walker_id)
            pipe.hdel(POS_KEY, walker_id)
            pipe.zrem(SEEN_KEY, walker_id)
        await pipe.execute()


async def position(redis, walker_id: str) -> tuple[float, float] | None:
    raw = await redis.hget(POS_KEY, walker_id)
    if raw is None:
        return None
    lat, lon = raw.split(",")
    return float(lat), float(lon)


async def positions(redis, walker_ids) -> dict[str, tuple[float, float]]:
    """Many positions in one round trip (pipelined HGETs), missing ids skipped.

    The per-fix fan-out and the sweeper need the coordinates of every neighbor
    at once; fetching them one HGET each turns k neighbors into k round trips.
    """
    walker_ids = list(walker_ids)
    if not walker_ids:
        return {}
    async with redis.pipeline(transaction=False) as pipe:
        for walker_id in walker_ids:
            pipe.hget(POS_KEY, walker_id)
        rows = await pipe.execute()
    out: dict[str, tuple[float, float]] = {}
    for walker_id, raw in zip(walker_ids, rows):
        if raw is None:
            continue
        lat, lon = raw.split(",")
        out[walker_id] = (float(lat), float(lon))
    return out


async def nearby(
    redis,
    lat: float,
    lon: float,
    radius_m: float,
    exclude_id: str | None = None,
) -> list[GeoHit]:
    """Walkers within ``radius_m`` of the point, nearest first.

    ``WITHCOORD`` returns each hit's coordinates with the search itself, so the
    whole result costs one round trip instead of one GEOSEARCH plus one HGET
    per hit.
    """
    rows = await redis.geosearch(
        GEO_KEY,
        longitude=lon,
        latitude=lat,
        radius=radius_m,
        unit="m",
        withdist=True,
        withcoord=True,
        sort="ASC",
    )

    hits: list[GeoHit] = []
    for row in rows:
        member, distance, coords = row[0], float(row[1]), row[2]
        if isinstance(member, bytes):
            member = member.decode()
        if member == exclude_id:
            continue
        hit_lon, hit_lat = coords  # GEOSEARCH returns (lon, lat)
        hits.append(
            GeoHit(
                walker_id=member,
                lat=float(hit_lat),
                lon=float(hit_lon),
                distance_m=round(distance, 1),
            )
        )
    return hits


async def stale_walkers(redis, ttl_seconds: float, now: float | None = None) -> list[str]:
    """Walkers whose last fix is older than ``ttl_seconds``.

    One ZRANGEBYSCORE over the stale slice of the seen index, rather than a
    read of every walker on every sweep.
    """
    now = time.time() if now is None else now
    stale = await redis.zrangebyscore(SEEN_KEY, "-inf", now - ttl_seconds)
    return [
        walker_id.decode() if isinstance(walker_id, bytes) else walker_id
        for walker_id in stale
    ]
