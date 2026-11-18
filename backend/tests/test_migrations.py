"""Keys whose Redis type changed under a live deployment.

The bug these cover is invisible to a test suite that starts from an empty
database: the first write creates the key with the new type, so everything
passes. It only appears on a database that already holds the old type, which is
every deployment that has been running — and it appears as a 500 on
``POST /auth/register``, not as a startup error.
"""

import asyncio

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import geo, invites, migrations
from app.main import create_app

PASSWORD = "correct-horse-battery"
EMAIL = "alice@example.com"


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def migrate(redis_client, geo_redis=None) -> list[str]:
    return asyncio.run(migrations.migrate_legacy_types(redis_client, geo_redis))


# --- dogwalk:invites:used, set -> sorted set --------------------------------


def test_a_legacy_used_set_is_converted(redis_client):
    asyncio.run(redis_client.sadd(invites.USED_KEY, "AAAAA-AAAAA", "BBBBB-BBBBB"))

    repaired = migrate(redis_client)

    assert invites.USED_KEY in repaired
    assert asyncio.run(redis_client.type(invites.USED_KEY)) == "zset"
    assert asyncio.run(redis_client.zcard(invites.USED_KEY)) == 2


def test_consuming_a_code_works_after_the_conversion(redis_client):
    """The write that used to raise WRONGTYPE."""
    asyncio.run(redis_client.sadd(invites.USED_KEY, "AAAAA-AAAAA"))
    migrate(redis_client)

    code = asyncio.run(invites.mint(redis_client))[0]

    assert asyncio.run(invites.consume(redis_client, code)) is True
    assert asyncio.run(invites.used_count(redis_client)) == 2


def test_the_conversion_is_idempotent(redis_client):
    asyncio.run(redis_client.sadd(invites.USED_KEY, "AAAAA-AAAAA"))

    assert migrate(redis_client) == [invites.USED_KEY]
    assert migrate(redis_client) == []
    assert asyncio.run(redis_client.zcard(invites.USED_KEY)) == 1


def test_an_absent_key_is_left_absent(redis_client):
    assert migrate(redis_client) == []
    assert asyncio.run(redis_client.type(invites.USED_KEY)) == "none"


# --- dogwalk:walkers:seen, hash -> sorted set -------------------------------


def test_a_legacy_seen_hash_keeps_its_scores(redis_client):
    """The sweeper reads these as last-seen times, so they must survive."""
    asyncio.run(redis_client.hset(geo.SEEN_KEY, mapping={"walker-a": "1000.5", "walker-b": "2000.25"}))

    repaired = migrate(redis_client)

    assert geo.SEEN_KEY in repaired
    assert asyncio.run(redis_client.type(geo.SEEN_KEY)) == "zset"
    assert asyncio.run(redis_client.zscore(geo.SEEN_KEY, "walker-a")) == 1000.5
    assert asyncio.run(redis_client.zscore(geo.SEEN_KEY, "walker-b")) == 2000.25


def test_the_seen_key_is_repaired_on_the_geo_client(redis_client):
    """With GEO_REDIS_URL set, the geo keys live on a different instance."""
    geo_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    asyncio.run(geo_client.hset(geo.SEEN_KEY, mapping={"walker-a": "1000.5"}))

    repaired = migrate(redis_client, geo_client)

    assert geo.SEEN_KEY in repaired
    assert asyncio.run(geo_client.type(geo.SEEN_KEY)) == "zset"
    assert asyncio.run(redis_client.type(geo.SEEN_KEY)) == "none"


# --- the regression, through the API ---------------------------------------


def test_registration_survives_a_legacy_used_set(redis_client):
    """A database from before the change must still be able to register.

    This is the shape of the production failure: the key exists as a set, the
    app boots, and the first registration answers 500.
    """
    asyncio.run(redis_client.sadd(invites.USED_KEY, "AAAAA-AAAAA"))
    code = asyncio.run(invites.mint(redis_client))[0]

    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=True)
    ) as client:
        response = client.post(
            "/auth/register",
            json={"email": EMAIL, "password": PASSWORD, "invite_code": code},
        )

    assert response.status_code == 201, response.text
    assert asyncio.run(redis_client.type(invites.USED_KEY)) == "zset"
