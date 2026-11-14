"""Blocking a walker.

Two things are being pinned down here. First the store: a block has to hide both
people from each other, because a one-way filter would leave the blocker on the
blocked walker's map, which is the exact thing they blocked them to prevent.
Second the enforcement: the filter runs on the server, before the frame is
built, so a blocked walker never receives the bearing at all rather than being
told to draw nothing.

The suite has no async plugin, so the coroutine tests follow the same
``asyncio.run`` pattern as ``test_invites.py``.
"""

import asyncio
import time

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import blocks, geo
from app.hub import Connection, Hub
from app.main import create_app
from app.proximity import refresh_neighborhood

BASE_LAT = 52.3702
BASE_LON = 4.8952
METERS_PER_DEGREE_LAT = 111_320.0
PASSWORD = "correct-horse-battery"

ALICE = "a" * 32
BOB = "b" * 32
CAROL = "c" * 32


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
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=False)
    ) as test_client:
        yield test_client


def sign_up(client, email: str) -> tuple[str, dict]:
    """Register a walker and return (user_id, auth headers)."""
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


# --- the store ---------------------------------------------------------------


def test_block_hides_both_directions(redis_client):
    async def scenario():
        await blocks.block(redis_client, ALICE, BOB, "Bob")

        assert await blocks.hidden_for(redis_client, ALICE) == {BOB}
        assert await blocks.hidden_for(redis_client, BOB) == {ALICE}

    asyncio.run(scenario())


def test_unblock_restores_both_directions(redis_client):
    async def scenario():
        await blocks.block(redis_client, ALICE, BOB, "Bob")
        await blocks.unblock(redis_client, ALICE, BOB)

        assert await blocks.hidden_for(redis_client, ALICE) == set()
        assert await blocks.hidden_for(redis_client, BOB) == set()

    asyncio.run(scenario())


def test_blocking_twice_is_harmless_and_updates_the_name(redis_client):
    async def scenario():
        await blocks.block(redis_client, ALICE, BOB, "Bob")
        await blocks.block(redis_client, ALICE, BOB, "Buddy")

        assert await blocks.blocked_by_me(redis_client, ALICE) == [(BOB, "Buddy")]
        assert await blocks.hidden_for(redis_client, BOB) == {ALICE}

    asyncio.run(scenario())


def test_unblocking_something_that_was_never_blocked_is_harmless(redis_client):
    async def scenario():
        await blocks.unblock(redis_client, ALICE, BOB)

        assert await blocks.hidden_for(redis_client, ALICE) == set()

    asyncio.run(scenario())


def test_cannot_block_yourself(redis_client):
    async def scenario():
        with pytest.raises(ValueError):
            await blocks.block(redis_client, ALICE, ALICE, "Me")

    asyncio.run(scenario())


def test_block_list_is_sorted_by_name(redis_client):
    async def scenario():
        await blocks.block(redis_client, ALICE, BOB, "Zoe")
        await blocks.block(redis_client, ALICE, CAROL, "adam")

        assert await blocks.blocked_by_me(redis_client, ALICE) == [
            (CAROL, "adam"),
            (BOB, "Zoe"),
        ]

    asyncio.run(scenario())


def test_forget_clears_both_directions(redis_client):
    async def scenario():
        await blocks.block(redis_client, ALICE, BOB, "Bob")
        await blocks.block(redis_client, BOB, CAROL, "Carol")

        await blocks.forget(redis_client, BOB)

        # Bob's own list is gone, and so is the entry he left in Alice's list.
        assert await blocks.blocked_by_me(redis_client, BOB) == []
        assert await blocks.blocked_by_me(redis_client, ALICE) == []
        # Carol blocked Bob, so she must stop hiding him too.
        assert await blocks.hidden_for(redis_client, CAROL) == set()

    asyncio.run(scenario())


def test_a_block_list_has_a_ceiling(redis_client):
    """Every block writes a key for the blocked id, so the list has to be bounded.

    An id that never belonged to an account is cleaned up by nothing except an
    unblock, which is why the ceiling is on the list rather than on the account.
    """

    async def scenario():
        for index in range(blocks.MAX_BLOCKS):
            await blocks.block(redis_client, ALICE, f"{index:032x}", f"Walker {index}")

        with pytest.raises(blocks.BlockListFull):
            await blocks.block(redis_client, ALICE, "f" * 32, "One too many")

        assert len(await blocks.blocked_by_me(redis_client, ALICE)) == blocks.MAX_BLOCKS

    asyncio.run(scenario())


def test_reblocking_at_the_ceiling_still_updates_the_name(redis_client):
    """Re-blocking writes no new entry, so it must not be refused."""

    async def scenario():
        for index in range(blocks.MAX_BLOCKS):
            await blocks.block(redis_client, ALICE, f"{index:032x}", f"Walker {index}")

        await blocks.block(redis_client, ALICE, f"{0:032x}", "Renamed")

        assert (f"{0:032x}", "Renamed") in await blocks.blocked_by_me(redis_client, ALICE)

    asyncio.run(scenario())


# --- enforcement -------------------------------------------------------------


def test_blocked_walker_is_dropped_from_the_alert(redis_client):
    async def scenario():
        hub = Hub()
        alice_socket = RecordingSocket()
        hub.add(Connection(walker_id=ALICE, name="Alice", websocket=alice_socket))

        await geo.upsert(redis_client, ALICE, BASE_LAT, BASE_LON)
        await geo.upsert(redis_client, BOB, north(100), BASE_LON)
        await blocks.block(redis_client, ALICE, BOB, "Bob")

        hits = await refresh_neighborhood(redis_client, hub, ALICE, BASE_LAT, BASE_LON, 500)

        assert hits == []
        assert alice_socket.sent[-1]["type"] == "PROXIMITY_CLEAR"

    asyncio.run(scenario())


def test_blocking_is_symmetric_on_the_map(redis_client):
    """Bob blocked Alice, so Alice must not see Bob either."""

    async def scenario():
        hub = Hub()
        alice_socket = RecordingSocket()
        hub.add(Connection(walker_id=ALICE, name="Alice", websocket=alice_socket))

        await geo.upsert(redis_client, ALICE, BASE_LAT, BASE_LON)
        await geo.upsert(redis_client, BOB, north(100), BASE_LON)
        await blocks.block(redis_client, BOB, ALICE, "Alice")

        hits = await refresh_neighborhood(redis_client, hub, ALICE, BASE_LAT, BASE_LON, 500)

        assert hits == []
        assert alice_socket.sent[-1]["type"] == "PROXIMITY_CLEAR"

    asyncio.run(scenario())


def test_unblocked_walker_comes_back(redis_client):
    async def scenario():
        hub = Hub()
        alice_socket = RecordingSocket()
        hub.add(Connection(walker_id=ALICE, name="Alice", websocket=alice_socket))

        await geo.upsert(redis_client, ALICE, BASE_LAT, BASE_LON)
        await geo.upsert(redis_client, BOB, north(100), BASE_LON)
        await blocks.block(redis_client, ALICE, BOB, "Bob")
        await blocks.unblock(redis_client, ALICE, BOB)

        hits = await refresh_neighborhood(redis_client, hub, ALICE, BASE_LAT, BASE_LON, 500)

        assert [hit.walker_id for hit in hits] == [BOB]
        assert [w["id"] for w in alice_socket.sent[-1]["walkers"]] == [BOB]

    asyncio.run(scenario())


def test_a_block_does_not_hide_anyone_else(redis_client):
    async def scenario():
        hub = Hub()
        alice_socket = RecordingSocket()
        hub.add(Connection(walker_id=ALICE, name="Alice", websocket=alice_socket))

        await geo.upsert(redis_client, ALICE, BASE_LAT, BASE_LON)
        await geo.upsert(redis_client, BOB, north(100), BASE_LON)
        await geo.upsert(redis_client, CAROL, north(200), BASE_LON)
        await blocks.block(redis_client, ALICE, BOB, "Bob")

        hits = await refresh_neighborhood(redis_client, hub, ALICE, BASE_LAT, BASE_LON, 500)

        assert [hit.walker_id for hit in hits] == [CAROL]

    asyncio.run(scenario())


# --- the endpoints -----------------------------------------------------------


def test_block_list_needs_a_token(client):
    assert client.get("/blocks").status_code == 401


def test_block_then_list_then_unblock(client):
    _, alice_headers = sign_up(client, "alice@example.com")
    bob_id, _ = sign_up(client, "bob@example.com")

    assert client.get("/blocks", headers=alice_headers).json() == []

    response = client.post(f"/blocks/{bob_id}", json={"name": "Bob"}, headers=alice_headers)
    assert response.status_code == 204

    assert client.get("/blocks", headers=alice_headers).json() == [{"id": bob_id, "name": "Bob"}]

    response = client.delete(f"/blocks/{bob_id}", headers=alice_headers)
    assert response.status_code == 204

    assert client.get("/blocks", headers=alice_headers).json() == []


def test_block_list_is_per_account(client):
    _, alice_headers = sign_up(client, "alice@example.com")
    _, bob_headers = sign_up(client, "bob@example.com")
    carol_id, _ = sign_up(client, "carol@example.com")

    client.post(f"/blocks/{carol_id}", json={"name": "Carol"}, headers=alice_headers)

    assert client.get("/blocks", headers=bob_headers).json() == []


def test_blocking_yourself_is_rejected(client):
    alice_id, alice_headers = sign_up(client, "alice@example.com")

    response = client.post(f"/blocks/{alice_id}", json={"name": "Me"}, headers=alice_headers)

    assert response.status_code == 400


def test_a_malformed_id_is_rejected_before_redis(client):
    _, alice_headers = sign_up(client, "alice@example.com")

    response = client.post("/blocks/not-a-walker-id", json={"name": "?"}, headers=alice_headers)

    assert response.status_code == 422


def test_blocking_an_unknown_walker_does_not_reveal_that_it_is_unknown(client):
    """Same answer either way, so this cannot be used to probe for accounts."""
    _, alice_headers = sign_up(client, "alice@example.com")

    response = client.post(f"/blocks/{'f' * 32}", json={"name": "Ghost"}, headers=alice_headers)

    assert response.status_code == 204


def test_a_full_block_list_answers_409(client):
    """The ceiling is reported as a property of the caller's own list.

    It says nothing about the id being blocked, so it is not the oracle that a
    404 would be.
    """
    _, alice_headers = sign_up(client, "alice@example.com")

    for index in range(blocks.MAX_BLOCKS):
        response = client.post(f"/blocks/{index:032x}", json={"name": "x"}, headers=alice_headers)
        assert response.status_code == 204

    response = client.post(f"/blocks/{'f' * 32}", json={"name": "Ghost"}, headers=alice_headers)

    assert response.status_code == 409


def test_a_block_survives_a_new_session(client):
    """The block lives in Redis, not in the token, so a reinstall keeps it."""
    _, alice_headers = sign_up(client, "alice@example.com")
    bob_id, _ = sign_up(client, "bob@example.com")
    client.post(f"/blocks/{bob_id}", json={"name": "Bob"}, headers=alice_headers)

    # A fresh login is what a reinstall looks like from the server's side.
    login = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert login.status_code == 200, login.text
    fresh_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/blocks", headers=fresh_headers).json() == [{"id": bob_id, "name": "Bob"}]


def test_deleting_an_account_clears_it_from_other_block_lists(client):
    _, alice_headers = sign_up(client, "alice@example.com")
    bob_id, bob_headers = sign_up(client, "bob@example.com")
    client.post(f"/blocks/{bob_id}", json={"name": "Bob"}, headers=alice_headers)

    response = client.request(
        "DELETE",
        "/auth/account",
        json={"password": PASSWORD},
        headers=bob_headers,
    )
    assert response.status_code == 204

    assert client.get("/blocks", headers=alice_headers).json() == []


def test_blocking_a_live_walker_updates_both_maps(client):
    """The point of the feature: the blocked walker stops receiving the bearing."""
    alice_id, alice_headers = sign_up(client, "alice@example.com")
    bob_id, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"

        bob.send_json(location(north(100)))
        assert [w["id"] for w in bob.receive_json()["walkers"]] == [alice_id]
        assert [w["id"] for w in alice.receive_json()["walkers"]] == [bob_id]

        response = client.post(f"/blocks/{bob_id}", json={"name": "Bob"}, headers=alice_headers)
        assert response.status_code == 204

        # Both maps are refreshed by the request, without waiting for a fix.
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"
        assert bob.receive_json()["type"] == "PROXIMITY_CLEAR"

        # And the next fix keeps it that way.
        bob.send_json(location(north(120)))
        assert bob.receive_json()["type"] == "PROXIMITY_CLEAR"
        assert alice.receive_json()["type"] == "PROXIMITY_CLEAR"


def test_unblocking_a_live_walker_brings_them_back(client):
    alice_id, alice_headers = sign_up(client, "alice@example.com")
    bob_id, bob_headers = sign_up(client, "bob@example.com")

    with client.websocket_connect("/ws/walk?name=Alice", headers=alice_headers) as alice, \
            client.websocket_connect("/ws/walk?name=Bob", headers=bob_headers) as bob:
        alice.receive_json()
        bob.receive_json()

        alice.send_json(location(BASE_LAT))
        alice.receive_json()
        bob.send_json(location(north(100)))
        bob.receive_json()
        alice.receive_json()

        client.post(f"/blocks/{bob_id}", json={"name": "Bob"}, headers=alice_headers)
        alice.receive_json()
        bob.receive_json()

        client.delete(f"/blocks/{bob_id}", headers=alice_headers)

        assert [w["id"] for w in alice.receive_json()["walkers"]] == [bob_id]
        assert [w["id"] for w in bob.receive_json()["walkers"]] == [alice_id]
