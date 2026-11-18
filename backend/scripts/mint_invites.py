"""Mint single-use invite codes.

Registration needs a code (see ``app/invites.py``), so this is how you hand one
to a friend. It talks to Redis directly rather than through the backend: minting
is an operator action, and putting it behind an HTTP endpoint would mean either
a second secret to manage or a route that mints accounts for anyone who finds it.

    python scripts/mint_invites.py            # one code
    python scripts/mint_invites.py --count 5  # a batch
    python scripts/mint_invites.py --list     # how many are unused

Reads ``REDIS_URL``, defaulting to the published port on the host.
"""

import argparse
import asyncio
import os
import pathlib
import sys

import redis.asyncio as aioredis

# Running as `python scripts/mint_invites.py` puts scripts/ on sys.path, not the
# backend root, so `app` is not importable until the parent is added.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import invites  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Mint Mikywalk invite codes.")
    parser.add_argument("--count", type=int, default=1, help="how many to mint (default 1)")
    parser.add_argument("--list", action="store_true", help="show the unused count and exit")
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis connection (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    args = parser.parse_args()

    client = aioredis.from_url(args.redis_url, decode_responses=True)
    try:
        if args.list:
            print(f"{await invites.available(client)} unused invite code(s)")
            return 0

        if args.count < 1:
            print("--count must be at least 1", file=sys.stderr)
            return 2

        codes = await invites.mint(client, args.count)
        for code in codes:
            print(code)
        print(f"\n{await invites.available(client)} unused code(s) remaining.", file=sys.stderr)
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
