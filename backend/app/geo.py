"""Redis GEO operations for the walker set.

Two keys are kept in step:

* ``GEO_KEY``  - a sorted set of walker ids scored by geohash, used for GEOSEARCH.
* ``POS_KEY``  - walker id -> "lat,lon", the exact coordinates the client reported.
* ``SEEN_KEY`` - walker id -> unix timestamp of the last fix, used by the sweeper.

Redis has no per-member TTL inside a sorted set, so expiry is enforced by the sweeper
in ``main.py``: members whose last fix is older than ``WALKER_TTL_SECONDS`` are removed.
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
        pipe.hset(SEEN_KEY, walker_id, now)
        await pipe.execute()


async def remove(redis, walker_id: str) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zrem(GEO_KEY, walker_id)
        pipe.hdel(POS_KEY, walker_id)
        pipe.hdel(SEEN_KEY, walker_id)
        await pipe.execute()


async def position(redis, walker_id: str) -> tuple[float, float] | None:
    raw = await redis.hget(POS_KEY, walker_id)
    if raw is None:
        return None
    lat, lon = raw.split(",")
    return float(lat), float(lon)


async def nearby(
    redis,
    lat: float,
    lon: float,
    radius_m: float,
    exclude_id: str | None = None,
) -> list[GeoHit]:
    """Walkers within ``radius_m`` of the point, nearest first."""
    rows = await redis.geosearch(
        GEO_KEY,
        longitude=lon,
        latitude=lat,
        radius=radius_m,
        unit="m",
        withdist=True,
        sort="ASC",
    )

    hits: list[GeoHit] = []
    for row in rows:
        member, distance = row[0], float(row[1])
        if isinstance(member, bytes):
            member = member.decode()
        if member == exclude_id:
            continue
        coords = await position(redis, member)
        if coords is None:
            continue
        hits.append(
            GeoHit(
                walker_id=member,
                lat=coords[0],
                lon=coords[1],
                distance_m=round(distance, 1),
            )
        )
    return hits


async def stale_walkers(redis, ttl_seconds: float, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    seen = await redis.hgetall(SEEN_KEY)
    stale: list[str] = []
    for walker_id, raw_ts in seen.items():
        if isinstance(walker_id, bytes):
            walker_id = walker_id.decode()
        if now - float(raw_ts) > ttl_seconds:
            stale.append(walker_id)
    return stale
