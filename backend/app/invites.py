"""Single-use invite codes.

Registration is the one endpoint that has to answer a question about an email
address: "is this taken?". Answering it truthfully is what makes the endpoint
useful, and it is also what makes it an enumeration oracle — anyone can ask
whether a given address has an account here.

Changing the status code does not fix that. If ``/auth/register`` always
answered 201, an attacker would register with a password of their choosing and
then try to log in with it: success means the address was free, failure means it
was taken. The oracle moves; it does not close. The only thing that closes it is
making an account unusable until its owner proves they control the address,
which is email verification.

For a friends-and-family deployment there is a cheaper answer that closes it
completely: require a code that only an invited person has. The check runs
*before* the email lookup, so a probe without a valid code gets the same 403
whether or not the address exists. Someone holding a code can still probe, but
holding a code means they were invited, which is the trust boundary we actually
want.

Codes are single use. ``SREM`` is atomic, so two simultaneous registrations
cannot both consume the same code.

Keys:

* ``dogwalk:invites``      - set of unused codes, normalised
* ``dogwalk:invites:used`` - sorted set of consumed codes scored by consumption
                             time, capped at ``MAX_USED_ENTRIES``. It exists for
                             the used-count in the panel and for refund symmetry;
                             nothing authorises on it, so evicting the oldest
                             entries is safe.
"""

import secrets
import time

UNUSED_KEY = "dogwalk:invites"
USED_KEY = "dogwalk:invites:used"

# The used set is bookkeeping, not a ledger: past this, the oldest consumed codes
# are evicted. Without a cap the set grows by one member per registration, forever.
MAX_USED_ENTRIES = 1000

# No I, O, 0 or 1: these get read aloud and typed on a phone, and the pairs are
# indistinguishable in most fonts.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
GROUP_SIZE = 5


def normalise(raw: str) -> str:
    """Canonical form of a code as typed by a human.

    Accepts ``abcde-fghij``, ``ABCDE FGHIJ`` and ``abcdefghij`` as the same code.
    Anything outside the alphabet is dropped, so a pasted code with stray
    punctuation still works.
    """
    return "".join(ch for ch in raw.upper() if ch in ALPHABET)


def format_code(code: str) -> str:
    groups = [code[i : i + GROUP_SIZE] for i in range(0, len(code), GROUP_SIZE)]
    return "-".join(groups)


def generate() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


async def mint(redis, count: int = 1) -> list[str]:
    """Create ``count`` unused codes and return them formatted for a human.

    Collisions are vanishingly unlikely at 50 bits, but a duplicate would
    silently shrink the batch, so the loop retries until the set actually grew.
    """
    if count < 1:
        return []

    codes: list[str] = []
    while len(codes) < count:
        candidate = generate()
        if await redis.sadd(UNUSED_KEY, candidate):
            codes.append(format_code(candidate))
    return codes


async def consume(redis, code: str) -> bool:
    """Spend a code. True if it was unused, False if it was unknown or spent."""
    normalised = normalise(code)
    if not normalised:
        return False
    if not await redis.srem(UNUSED_KEY, normalised):
        return False
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(USED_KEY, {normalised: time.time()})
        # Keep only the newest MAX_USED_ENTRIES, in the same transaction as the
        # add, so the set can never outgrow the cap.
        pipe.zremrangebyrank(USED_KEY, 0, -(MAX_USED_ENTRIES + 1))
        await pipe.execute()
    return True


async def refund(redis, code: str) -> None:
    """Put a spent code back.

    Registration consumes the code before it knows whether the email is free,
    because checking the email first would reintroduce the oracle. When the
    email turns out to be taken the registration did not happen, so the code
    goes back rather than being burnt on a typo. Removing it from the used set
    is harmless when the cap already evicted it.
    """
    normalised = normalise(code)
    if not normalised:
        return
    await redis.sadd(UNUSED_KEY, normalised)
    await redis.zrem(USED_KEY, normalised)


async def available(redis) -> int:
    return int(await redis.scard(UNUSED_KEY))


async def used_count(redis) -> int:
    """How many consumed codes are still remembered (capped, see MAX_USED_ENTRIES)."""
    return int(await redis.zcard(USED_KEY))
