import asyncio
import time
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import geo
from app.hub import Connection, Hub
from app.main import create_app, sweep_once
from app.proximity import refresh_neighborhood

BASE_LAT = 52.3702
BASE_LON = 4.8952
METERS_PER_DEGREE_LAT = 111_320.0
PASSWORD = "correct-horse-battery"


def north(meters: float) -> float:
    return BASE_LAT + meters / METERS_PER_DEGREE_LAT


def location(lat: float, lon: float = BASE_LON) -> dict:
    return {
        "type": "location",
        "lat": lat,
        "lon": lon,
        "accuracy": 5.0,
        "ts": int(time.time() * 1000),
    }


class RecordingSocket:
    """Stands in for a WebSocket so hub sends can be asserted without a server."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(create_app(redis_client=redis_client, rate_limit=False, invite_required=False)) as test_client:
        yield test_client


def sign_up(client, email: str) -> tuple[str, dict]:
    """Register a walker and return (user_id, auth headers)."""
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


def test_welcome_frame_carries_radius(client):
    alice_id, alice_headers = sign_up(client, "alice@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice:
        frame = alice.receive_json()

    assert frame["type"] == "welcome"
    assert frame["walker_id"] == alice_id
    assert frame["radius_m"] == 500


def test_walkers_inside_radius_alert_each_other(client):
    alice_id, alice_headers = sign_up(client, "alice@example.com")
    bob_id, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"

        bob.send_json(location(north(100)))

        bob_alert = bob.receive_json()
        assert bob_alert["type"] == "PROXIMITY_ALERT"
        assert [w["id"] for w in bob_alert["walkers"]] == [alice_id]
        assert bob_alert["walkers"][0]["name"] == "Alice"
        assert 90 < bob_alert["walkers"][0]["distance_m"] < 110

        alice_alert = alice.receive_json()
        assert alice_alert["type"] == "PROXIMITY_ALERT"
        assert [w["id"] for w in alice_alert["walkers"]] == [bob_id]


def test_walker_leaving_radius_clears_both_maps(client):
    _, alice_headers = sign_up(client, "alice@example.com")
    _, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        alice.receive_json()

        bob.send_json(location(north(100)))
        bob.receive_json()
        alice.receive_json()

        # Bob walks 2 km away: he loses Alice, and Alice must lose him too.
        bob.send_json(location(north(2000)))

        assert bob.receive_json()["type"] == "PROXIMITY_CLEAR"
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"


def test_disconnect_clears_the_watcher(client):
    _, alice_headers = sign_up(client, "alice@example.com")
    _, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice:
        alice.receive_json()

        with client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
            bob.receive_json()
            alice.send_json(location(BASE_LAT))
            alice.receive_json()
            bob.send_json(location(north(100)))
            bob.receive_json()
            alice.receive_json()

        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"


def test_invalid_frame_is_rejected_without_dropping_the_socket(client):
    _, alice_headers = sign_up(client, "alice@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice:
        alice.receive_json()

        alice.send_json({"type": "location", "lat": 999.0, "lon": BASE_LON})
        error = alice.receive_json()
        assert error["type"] == "error"

        alice.send_json(location(BASE_LAT))
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"


def test_sweep_expires_stale_walkers_and_clears_watchers():
    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hub = Hub()
        alice_socket = RecordingSocket()
        bob_socket = RecordingSocket()
        hub.add(Connection("alice", "Alice", alice_socket))
        hub.add(Connection("bob", "Bob", bob_socket))

        await geo.upsert(redis, "alice", BASE_LAT, BASE_LON)
        await geo.upsert(redis, "bob", north(100), BASE_LON)
        await refresh_neighborhood(redis, hub, "alice", BASE_LAT, BASE_LON, 500)
        await refresh_neighborhood(redis, hub, "bob", north(100), BASE_LON, 500)
        alice_socket.sent.clear()
        bob_socket.sent.clear()

        # Bob's last fix is older than the 30 s TTL.
        await redis.zadd(geo.SEEN_KEY, {"bob": time.time() - 120})

        app = SimpleNamespace(state=SimpleNamespace(redis=redis, geo_redis=redis, hub=hub))
        expired = await sweep_once(app)
        return expired, alice_socket.sent, bob_socket.sent

    expired, alice_sent, bob_sent = asyncio.run(scenario())

    assert expired == ["bob"]
    assert alice_sent[-1]["type"] == "PROXIMITY_CLEAR"
    assert bob_sent == []


def test_sweep_only_refreshes_watchers_of_the_expired_walker():
    """A sweep can only change the view of walkers who were watching an expired
    one; everyone else must not get a frame (that is what keeps the periodic
    sweep O(expired) instead of O(connected))."""

    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hub = Hub()
        sockets = {
            name: RecordingSocket() for name in ("alice", "bob", "carol")
        }
        for name, socket in sockets.items():
            hub.add(Connection(name, name.capitalize(), socket))

        await geo.upsert(redis, "alice", BASE_LAT, BASE_LON)
        await geo.upsert(redis, "bob", north(100), BASE_LON)
        await geo.upsert(redis, "carol", north(5000), BASE_LON)  # far outside 500 m
        await refresh_neighborhood(redis, hub, "alice", BASE_LAT, BASE_LON, 500)
        await refresh_neighborhood(redis, hub, "bob", north(100), BASE_LON, 500)
        await refresh_neighborhood(redis, hub, "carol", north(5000), BASE_LON, 500)
        for socket in sockets.values():
            socket.sent.clear()

        await redis.zadd(geo.SEEN_KEY, {"bob": time.time() - 120})

        app = SimpleNamespace(state=SimpleNamespace(redis=redis, geo_redis=redis, hub=hub))
        expired = await sweep_once(app)
        return expired, {name: s.sent for name, s in sockets.items()}

    expired, sent = asyncio.run(scenario())

    assert expired == ["bob"]
    # Alice was watching Bob: her map is cleared.
    assert sent["alice"][-1]["type"] == "PROXIMITY_CLEAR"
    # Bob watched nobody stale, Carol watched nobody at all: no frames for them.
    assert sent["bob"] == []
    assert sent["carol"] == []


def test_refresh_neighborhood_records_last_neighbors():
    """last_neighbors must reflect what the walker was last shown, otherwise the
    sweeper cannot tell which connections an expired walker was visible to."""

    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        hub = Hub()
        hub.add(Connection("alice", "Alice", RecordingSocket()))
        hub.add(Connection("bob", "Bob", RecordingSocket()))
        await geo.upsert(redis, "alice", BASE_LAT, BASE_LON)
        await geo.upsert(redis, "bob", north(100), BASE_LON)
        await refresh_neighborhood(redis, hub, "alice", BASE_LAT, BASE_LON, 500)
        return hub.get("alice").last_neighbors

    assert asyncio.run(scenario()) == {"bob"}


def test_geo_positions_and_remove_many_are_batched_helpers():
    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await geo.upsert(redis, "a", BASE_LAT, BASE_LON)
        await geo.upsert(redis, "b", north(100), BASE_LON)
        coords = await geo.positions(redis, ["a", "b", "gone"])
        await geo.remove_many(redis, ["a", "b"])
        return coords, await geo.positions(redis, ["a", "b"])

    coords, after = asyncio.run(scenario())

    assert coords["a"] == pytest.approx((BASE_LAT, BASE_LON))
    assert coords["b"][0] == pytest.approx(north(100))
    assert "gone" not in coords
    assert after == {}


def test_alert_carries_no_coordinates(client):
    """A neighbour is a distance and a bearing, never a place.

    This is the whole point of the privacy change: a client that logs every frame
    learns "someone is about 100 m north", not "someone is at 52.3711, 4.8952".
    """
    _, alice_headers = sign_up(client, "alice@example.com")
    _, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        alice.receive_json()

        bob.send_json(location(north(100)))
        bob.receive_json()
        alert = alice.receive_json()

    walker = alert["walkers"][0]
    assert "lat" not in walker
    assert "lon" not in walker
    assert walker["distance_m"] == 100.0
    # Bob is due north of Alice.
    assert walker["bearing_deg"] == 0.0


def test_alert_distance_is_quantised(client):
    """A distance that moves in 1 m steps would be a rangefinder."""
    _, alice_headers = sign_up(client, "alice@example.com")
    _, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        alice.receive_json()

        bob.send_json(location(north(112)))
        bob.receive_json()
        alert = alice.receive_json()

    # 112 m snaps to the 100 m bucket, not to 112.
    assert alert["walkers"][0]["distance_m"] == 100.0


def test_geo_nearby_excludes_self_and_sorts_by_distance():
    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        await geo.upsert(redis, "self", BASE_LAT, BASE_LON)
        await geo.upsert(redis, "far", north(400), BASE_LON)
        await geo.upsert(redis, "near", north(50), BASE_LON)
        await geo.upsert(redis, "outside", north(900), BASE_LON)
        return await geo.nearby(redis, BASE_LAT, BASE_LON, 500, exclude_id="self")

    hits = asyncio.run(scenario())

    assert [hit.walker_id for hit in hits] == ["near", "far"]
    assert hits[0].distance_m < hits[1].distance_m


def test_geo_nearby_gets_coordinates_from_the_search_itself():
    """WITHCOORD: a hit is returned with its coordinates even when it has no
    POS_KEY entry, instead of being dropped by a per-hit lookup (the old N+1)."""

    async def scenario():
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        # GEOADD only, no POS_KEY write.
        await redis.geoadd(geo.GEO_KEY, (BASE_LON, BASE_LAT, "self"))
        await redis.geoadd(geo.GEO_KEY, (BASE_LON, north(50), "near"))
        return await geo.nearby(redis, BASE_LAT, BASE_LON, 500, exclude_id="self")

    hits = asyncio.run(scenario())

    assert [hit.walker_id for hit in hits] == ["near"]
    assert 45 < hits[0].distance_m < 55
    # Geohash precision is sub-metre, so 1e-5 degrees (~1 m) is a safe bound.
    assert hits[0].lat == pytest.approx(north(50), abs=1e-5)
