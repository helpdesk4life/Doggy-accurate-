"""Limits on the walk websocket: handshakes per address, frame size, frame rate.

The http middleware in ``app/ratelimit.py`` never sees websocket scope, so
``/ws/walk`` enforces its own bucket before accepting the handshake, plus a
per-connection floor on how often a location frame is processed and a cap on
frame size. A client that floods the socket must not be able to make the server
run a GEOSEARCH fan-out per frame.
"""

import asyncio
import time

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import geo, ratelimit, ws
from app.main import create_app

PASSWORD = "correct-horse-battery"
BASE_LAT = 52.3702
BASE_LON = 4.8952


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def make_client(redis_client, rate_limit: bool) -> TestClient:
    return TestClient(
        create_app(redis_client=redis_client, rate_limit=rate_limit, invite_required=False)
    )


def sign_up(client, email: str) -> tuple[str, dict]:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


def location(lat: float, lon: float = BASE_LON) -> dict:
    return {
        "type": "location",
        "lat": lat,
        "lon": lon,
        "accuracy": 5.0,
        "ts": int(time.time() * 1000),
    }


def stored_position(redis_client, user_id: str) -> tuple[float, float]:
    return asyncio.run(geo.position(redis_client, user_id))


def test_walk_socket_handshakes_are_throttled_per_address(
    redis_client, frozen_window, monkeypatch
):
    monkeypatch.setitem(ratelimit.LIMITS, "/ws/walk", ratelimit.Limit(3, 60))
    with make_client(redis_client, rate_limit=True) as client:
        _, headers = sign_up(client, "alice@example.com")
        flooder = {**headers, "X-Forwarded-For": "203.0.113.9"}

        for _ in range(3):
            with client.websocket_connect("/ws/walk", headers=flooder) as sock:
                assert sock.receive_json()["type"] == "welcome"

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/walk", headers=flooder):
                pass

        # The bucket is per address: a different one is unaffected.
        other = {**headers, "X-Forwarded-For": "198.51.100.7"}
        with client.websocket_connect("/ws/walk", headers=other) as sock:
            assert sock.receive_json()["type"] == "welcome"


def test_handshake_limit_is_off_when_the_limiter_is_off(redis_client, monkeypatch):
    monkeypatch.setitem(ratelimit.LIMITS, "/ws/walk", ratelimit.Limit(2, 60))
    with make_client(redis_client, rate_limit=False) as client:
        _, headers = sign_up(client, "alice@example.com")
        for _ in range(4):
            with client.websocket_connect("/ws/walk", headers=headers) as sock:
                assert sock.receive_json()["type"] == "welcome"


def test_frames_inside_the_interval_are_held_not_applied(redis_client, monkeypatch):
    # The floor bounds evaluation to one fix per interval: with a 5 s interval,
    # rapid follow-up fixes must not be applied yet. The first fix always passes
    # because last_fix_at starts at 0.0.
    monkeypatch.setattr(ws, "MIN_FRAME_INTERVAL_SECONDS", 5.0)
    with make_client(redis_client, rate_limit=False) as client:
        user_id, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as sock:
            sock.receive_json()  # welcome
            sock.send_json(location(BASE_LAT + 0.001))
            sock.send_json(location(BASE_LAT + 0.002))
            sock.send_json(location(BASE_LAT + 0.003))
            assert sock.receive_json()["type"] == "PROXIMITY_CLEAR"  # first fix only

            # Asserted inside the socket: on disconnect the handler removes the
            # walker's geo entry, so the position is gone after the with-block.
            lat, _ = stored_position(redis_client, user_id)
            assert lat == pytest.approx(BASE_LAT + 0.001)  # later fixes still pending


def test_a_pending_fix_is_applied_when_the_interval_elapses(redis_client, monkeypatch):
    # Coalescing: the newest pending fix is applied once the interval allows, so
    # a fast movement is throttled but never dropped.
    monkeypatch.setattr(ws, "MIN_FRAME_INTERVAL_SECONDS", 0.2)
    with make_client(redis_client, rate_limit=False) as client:
        user_id, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as sock:
            sock.receive_json()  # welcome
            sock.send_json(location(BASE_LAT + 0.001))
            sock.send_json(location(BASE_LAT + 0.002))
            assert sock.receive_json()["type"] == "PROXIMITY_CLEAR"  # first fix, immediate
            assert sock.receive_json()["type"] == "PROXIMITY_CLEAR"  # pending fix, after ~0.2 s

            lat, _ = stored_position(redis_client, user_id)
            assert lat == pytest.approx(BASE_LAT + 0.002)  # newest wins


def test_every_frame_is_processed_when_the_interval_is_zero(redis_client, monkeypatch):
    monkeypatch.setattr(ws, "MIN_FRAME_INTERVAL_SECONDS", 0.0)
    with make_client(redis_client, rate_limit=False) as client:
        user_id, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as sock:
            sock.receive_json()  # welcome
            sock.send_json(location(BASE_LAT + 0.001))
            assert sock.receive_json()["type"] == "PROXIMITY_CLEAR"
            sock.send_json(location(BASE_LAT + 0.002))
            assert sock.receive_json()["type"] == "PROXIMITY_CLEAR"

            lat, _ = stored_position(redis_client, user_id)
            assert lat == pytest.approx(BASE_LAT + 0.002)


def test_oversized_frame_closes_the_socket(redis_client, monkeypatch):
    monkeypatch.setattr(ws, "MAX_FRAME_BYTES", 16)
    with make_client(redis_client, rate_limit=False) as client:
        _, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as sock:
            sock.receive_json()  # welcome
            sock.send_text("x" * 4096)
            with pytest.raises(WebSocketDisconnect) as exc:
                sock.receive_json()
            assert exc.value.code == ws.MESSAGE_TOO_BIG


def test_a_second_socket_for_the_same_walker_replaces_the_first(redis_client):
    with make_client(redis_client, rate_limit=False) as client:
        user_id, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as first:
            assert first.receive_json()["type"] == "welcome"
            with client.websocket_connect("/ws/walk", headers=headers) as second:
                assert second.receive_json()["type"] == "welcome"

                with pytest.raises(WebSocketDisconnect) as exc:
                    first.receive_json()
                assert exc.value.code == ws.REPLACED

                # The live session still owns the hub entry and the geo position:
                # a fix from the new socket is applied, and the old socket's
                # disconnect must not remove them.
                second.send_json(location(BASE_LAT))
                assert second.receive_json()["type"] == "PROXIMITY_CLEAR"
                lat, _ = stored_position(redis_client, user_id)
                assert lat == pytest.approx(BASE_LAT)

        # Both sockets are gone now, so the position is removed.
        assert stored_position(redis_client, user_id) is None


def test_malformed_json_still_gets_an_error_frame(redis_client):
    with make_client(redis_client, rate_limit=False) as client:
        _, headers = sign_up(client, "alice@example.com")
        with client.websocket_connect("/ws/walk", headers=headers) as sock:
            sock.receive_json()  # welcome
            sock.send_text("not json")
            frame = sock.receive_json()
            assert frame["type"] == "error"
            assert frame["message"] == "malformed JSON"
