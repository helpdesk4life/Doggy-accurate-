"""Create an operator account for the admin API.

Admins are a separate store from walkers and are deliberately not created
through an endpoint: an endpoint that creates admins has to be protected by an
admin, which is a bootstrapping problem. This is the same shape as
``scripts/mint_invites.py`` — operator actions are scripts.

    python scripts/create_admin.py --email you@example.com
    python scripts/create_admin.py --list
    python scripts/create_admin.py --backfill

``--backfill`` fills ``dogwalk:users:created`` for accounts that were registered
before that index existed. Without it those accounts are invisible to
``GET /admin/users``, because the list pages over the index rather than scanning
the keyspace.

Reads ``REDIS_URL``, defaulting to the published port on the host.
"""

import argparse
import asyncio
import getpass
import os
import pathlib
import re
import sys

import redis.asyncio as aioredis

# Running as `python scripts/create_admin.py` puts scripts/ on sys.path, not the
# backend root, so `app` is not importable until the parent is added.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import admin, users  # noqa: E402

# Ids are uuid4().hex, so the pattern is exact. It also keeps
# `dogwalk:users:email:*` and `dogwalk:users:<id>:refresh` out of the scan.
USER_ID_KEY = re.compile(r"^dogwalk:users:[0-9a-f]{32}$")
ADMIN_ID_KEY = re.compile(r"^dogwalk:admins:[0-9a-f]{32}$")


async def list_admins(client) -> int:
    found = []
    async for key in client.scan_iter("dogwalk:admins:*"):
        if ADMIN_ID_KEY.match(key):
            found.append(key.rsplit(":", 1)[1])

    if not found:
        print("no admin accounts")
        return 0

    for admin_id in sorted(found):
        record = await admin.get_by_id(client, admin_id)
        if record is not None:
            print(f"{record['email']}\t{admin_id}\tcreated {record['created_at']}")
    return 0


async def backfill(client) -> int:
    added = 0
    async for key in client.scan_iter("dogwalk:users:*"):
        if not USER_ID_KEY.match(key):
            continue
        created_at = await client.hget(key, "created_at")
        if created_at is None:
            continue
        added += await client.zadd(
            users.CREATED_INDEX_KEY, {key.rsplit(":", 1)[1]: int(created_at)}
        )

    print(f"{added} account(s) added; {await users.count_users(client)} indexed")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Create a DogWalk admin account.")
    parser.add_argument("--email", help="admin email")
    parser.add_argument(
        "--password",
        help="password; omit to be prompted (a password on the command line lands in your shell history)",
    )
    parser.add_argument("--list", action="store_true", help="list admin accounts and exit")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="index existing accounts for the admin list and exit",
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis connection (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    args = parser.parse_args()

    client = aioredis.from_url(args.redis_url, decode_responses=True)
    try:
        if args.list:
            return await list_admins(client)
        if args.backfill:
            return await backfill(client)

        if not args.email:
            print("--email is required", file=sys.stderr)
            return 2

        password = args.password
        if password is None:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat: "):
                print("passwords do not match", file=sys.stderr)
                return 2
        if len(password) < 8:
            print("password must be at least 8 characters", file=sys.stderr)
            return 2

        try:
            record = await admin.create_admin(client, args.email, password)
        except admin.EmailTaken:
            print(f"{args.email} already has an admin account", file=sys.stderr)
            return 1

        print(f"admin {record['email']} created: {record['admin_id']}")
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
