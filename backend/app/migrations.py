"""Repairs for keys whose Redis type changed under a live deployment.

A key's type is part of its schema, and Redis refuses an operation against the
wrong one: ``ZADD`` on a set raises ``WRONGTYPE`` rather than converting. Two
keys changed type in the 2026-11-17 hardening pass, and both sit on a path a
user can reach:

* ``dogwalk:invites:used`` - set -> sorted set, so the cap can evict the oldest
  entries with ``ZREMRANGEBYRANK``. ``invites.consume()`` writes it on every
  registration, so a database that predates the change answers 500 to
  ``POST /auth/register`` until the key is converted.
* ``dogwalk:walkers:seen`` - hash -> sorted set, so the sweeper is one
  ``ZRANGEBYSCORE`` instead of an ``HGETALL`` over every walker. Reached only
  when the geo index shares the persistent Redis, i.e. when ``GEO_REDIS_URL``
  is unset.

CI could not catch either one: it starts against an empty Redis, where the first
write creates the key with the new type. The failure exists only on a database
that already holds the old one, which is every deployment that has been running.

The repair runs at startup, before the sweeper, and is idempotent: it reads the
type first and does nothing when the key is already correct or absent.
"""

import logging
import time

from app import geo, invites

logger = logging.getLogger(__name__)


async def _set_to_zset(redis, key: str, score: float) -> bool:
    """Convert a set to a sorted set, scoring every member the same.

    The old set carried no timestamps, so there is no order to preserve. Scoring
    them all at ``score`` puts them behind anything consumed later, which is what
    the cap wants: the legacy entries are the oldest and are evicted first.
    """
    if await redis.type(key) != "set":
        return False

    members = await redis.smembers(key)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        if members:
            pipe.zadd(key, {member: score for member in members})
        await pipe.execute()
    return True


async def _hash_to_zset(redis, key: str) -> bool:
    """Convert a hash of member -> numeric score to a sorted set.

    The scores are kept, not replaced: the sweeper reads them as last-seen
    timestamps, so scoring them all at "now" would keep every stale walker alive
    for another TTL.
    """
    if await redis.type(key) != "hash":
        return False

    entries = await redis.hgetall(key)
    scores: dict[str, float] = {}
    for member, raw in entries.items():
        try:
            scores[member] = float(raw)
        except (TypeError, ValueError):
            # Nothing writes a non-numeric score, so this is a corrupt entry
            # rather than a format to support. Dropping it expires the walker,
            # which is the safe direction.
            logger.warning("dropping unparseable score for %s in %s", member, key)

    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        if scores:
            pipe.zadd(key, scores)
        await pipe.execute()
    return True


async def migrate_legacy_types(redis, geo_redis=None) -> list[str]:
    """Repair every key whose type changed under a live deployment.

    Returns the keys that were converted, so the caller can log them. An empty
    list is the normal case on a fresh database and on every boot after the
    first.
    """
    repaired: list[str] = []

    if await _set_to_zset(redis, invites.USED_KEY, time.time()):
        repaired.append(invites.USED_KEY)

    if await _hash_to_zset(geo_redis if geo_redis is not None else redis, geo.SEEN_KEY):
        repaired.append(geo.SEEN_KEY)

    return repaired
