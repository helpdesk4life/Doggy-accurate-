"""Server-side block list.

Blocking is the one social signal that cannot live on the device. Friend and
disliked only change how a marker looks and sounds, so the phone keeps them to
itself; a block has to stop the other walker receiving your bearing at all, and
only the server can do that.

Invisibility is symmetric. If A blocks B, neither sees the other: a one-way
filter would leave A on B's map, which is the exact thing A blocked them to
prevent. Two keys per user keep that true from either side:

* ``dogwalk:blocks:<id>``     - hash of blocked id -> the name shown in settings
* ``dogwalk:blocked_by:<id>`` - set of ids that blocked this user

The name is stored because the settings list has to name people who are not
walking right now, and the hub only knows the names of live connections. It is
the same display name the server already broadcasts to everyone in range, so
keeping a copy exposes nothing new.

Blocks are not given a TTL. "Survives reinstall" is the whole point, and a block
that quietly expired after a month would be worse than no block at all.
"""

BLOCKS_KEY = "dogwalk:blocks:{}"
BLOCKED_BY_KEY = "dogwalk:blocked_by:{}"

# A block list is a personal setting, not a feed. The ceiling exists because
# every block also writes a key for the blocked id, and an id that never
# belonged to an account is cleaned up by nothing except an unblock. Without a
# limit, a signed-in account could mint keys until Redis ran out of memory.
MAX_BLOCKS = 200


class BlockListFull(Exception):
    """The walker already has ``MAX_BLOCKS`` entries."""


def _blocks_key(walker_id: str) -> str:
    return BLOCKS_KEY.format(walker_id)


def _blocked_by_key(walker_id: str) -> str:
    return BLOCKED_BY_KEY.format(walker_id)


async def block(redis, blocker_id: str, blocked_id: str, name: str = "") -> None:
    """Hide ``blocker_id`` and ``blocked_id`` from each other, both ways.

    Idempotent: blocking twice leaves the same two keys, and re-blocking with a
    fresh name updates the name the settings list shows.

    Raises ``BlockListFull`` once the blocker holds ``MAX_BLOCKS`` entries. The
    check is not atomic with the write below, so two concurrent blocks can
    overshoot by one; a soft ceiling is enough, because the point is that the
    number is bounded rather than that it is exact.
    """
    if blocker_id == blocked_id:
        raise ValueError("cannot block yourself")

    key = _blocks_key(blocker_id)
    # Re-blocking an existing entry only updates its name, so it must not count
    # against the ceiling.
    if await redis.hlen(key) >= MAX_BLOCKS and not await redis.hexists(key, blocked_id):
        raise BlockListFull(f"a block list holds at most {MAX_BLOCKS} walkers")

    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, blocked_id, name)
        pipe.sadd(_blocked_by_key(blocked_id), blocker_id)
        await pipe.execute()


async def unblock(redis, blocker_id: str, blocked_id: str) -> None:
    """Undo a block. Harmless when there was none."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hdel(_blocks_key(blocker_id), blocked_id)
        pipe.srem(_blocked_by_key(blocked_id), blocker_id)
        await pipe.execute()


async def hidden_for(redis, walker_id: str) -> set[str]:
    """Everyone this walker must not see: who they blocked, and who blocked them.

    One round trip, because this runs on every proximity evaluation.
    """
    async with redis.pipeline(transaction=False) as pipe:
        pipe.hkeys(_blocks_key(walker_id))
        pipe.smembers(_blocked_by_key(walker_id))
        blocked, blocked_by = await pipe.execute()
    return set(blocked) | set(blocked_by)


async def blocked_by_me(redis, walker_id: str) -> list[tuple[str, str]]:
    """The walker's own block list as (id, name), sorted for a stable UI."""
    entries = await redis.hgetall(_blocks_key(walker_id))
    return sorted(entries.items(), key=lambda item: (item[1].lower(), item[0]))


async def forget(redis, walker_id: str) -> None:
    """Drop both keys for a deleted account, and the reverse entries pointing at it.

    Called from account deletion. Without the second half, everyone who blocked
    the deleted walker would keep a stale id in their list forever.
    """
    blocked = await redis.hkeys(_blocks_key(walker_id))
    blocked_by = await redis.smembers(_blocked_by_key(walker_id))

    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(_blocks_key(walker_id), _blocked_by_key(walker_id))
        for other_id in blocked:
            pipe.srem(_blocked_by_key(other_id), walker_id)
        for other_id in blocked_by:
            pipe.hdel(_blocks_key(other_id), walker_id)
        await pipe.execute()
