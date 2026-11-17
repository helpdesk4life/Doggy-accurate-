"""Invite codes, and the enumeration gap they close.

The property worth protecting is not "a code is required" — it is that
``/auth/register`` answers the same way for a taken address and a free one when
the caller has no code. If that ever stops being true, the endpoint is an
account-enumeration oracle again.
"""

import asyncio

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import invites
from app.main import create_app

PASSWORD = "correct-horse-battery"
EMAIL = "alice@example.com"
OTHER = "bob@example.com"


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=True)
    ) as test_client:
        yield test_client


@pytest.fixture
def open_client(redis_client):
    """The same app with the gate off, for the public-launch path."""
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=False)
    ) as test_client:
        yield test_client


def mint(redis_client, count: int = 1) -> list[str]:
    return asyncio.run(invites.mint(redis_client, count))


def register(client, email: str = EMAIL, code: str | None = None, password: str = PASSWORD):
    payload = {"email": email, "password": password}
    if code is not None:
        payload["invite_code"] = code
    return client.post("/auth/register", json=payload)


# --- the gate ---------------------------------------------------------------


def test_register_without_a_code_is_refused(client):
    response = register(client)

    assert response.status_code == 403
    assert "invite" in response.json()["detail"]


def test_register_with_an_unknown_code_is_refused(client):
    response = register(client, code="AAAAA-AAAAA")

    assert response.status_code == 403


def test_register_with_a_minted_code_succeeds(client, redis_client):
    code = mint(redis_client)[0]

    response = register(client, code=code)

    assert response.status_code == 201, response.text
    assert response.json()["access_token"]


def test_a_code_is_single_use(client, redis_client):
    code = mint(redis_client)[0]

    assert register(client, code=code).status_code == 201
    assert register(client, email=OTHER, code=code).status_code == 403


def test_a_code_survives_a_mistyped_email(client, redis_client):
    """A 409 did not create an account, so the code must not be burnt."""
    assert register(client, code=mint(redis_client)[0]).status_code == 201

    # A fresh code, spent on an address that is already taken.
    second = mint(redis_client)[0]
    assert register(client, code=second).status_code == 409

    # The failed registration refunded it, so it still works for someone else.
    assert register(client, email=OTHER, code=second).status_code == 201


def test_code_formatting_is_forgiving(client, redis_client):
    code = mint(redis_client)[0]

    # Lower case, no dash, and a stray space all describe the same code.
    response = register(client, code=code.replace("-", "").lower() + " ")

    assert response.status_code == 201, response.text


# --- the property the gate exists for ---------------------------------------


def test_taken_and_free_addresses_are_indistinguishable_without_a_code(client, redis_client):
    """The whole point: no code means no answer about the address."""
    code = mint(redis_client)[0]
    assert register(client, code=code).status_code == 201

    taken = register(client, email=EMAIL)
    free = register(client, email="nobody@example.com")

    assert taken.status_code == free.status_code == 403
    assert taken.json() == free.json()


def test_a_bad_code_does_not_reveal_whether_the_address_exists(client, redis_client):
    code = mint(redis_client)[0]
    assert register(client, code=code).status_code == 201

    taken = register(client, email=EMAIL, code="AAAAA-AAAAA")
    free = register(client, email="nobody@example.com", code="AAAAA-AAAAA")

    assert taken.status_code == free.status_code == 403
    assert taken.json() == free.json()


def test_the_gate_can_be_turned_off(open_client):
    response = register(open_client)

    assert response.status_code == 201, response.text


# --- the module itself ------------------------------------------------------


def test_mint_returns_distinct_formatted_codes(redis_client):
    codes = mint(redis_client, 5)

    assert len(set(codes)) == 5
    for code in codes:
        assert len(code) == 11  # 10 characters plus the group separator
        assert code[5] == "-"


def test_normalise_strips_punctuation_and_case():
    assert invites.normalise("abcde-fghjk") == "ABCDEFGHJK"
    assert invites.normalise("ABCDE FGHJK") == "ABCDEFGHJK"
    assert invites.normalise("  abcde_fghjk  ") == "ABCDEFGHJK"
    assert invites.normalise("") == ""


def test_normalise_drops_characters_outside_the_alphabet():
    # I, O, 0 and 1 are not in the alphabet, so they cannot be part of a code.
    assert invites.normalise("AB0CD1EF") == "ABCDEF"


def test_consume_and_refund_round_trip(redis_client):
    code = mint(redis_client)[0]

    assert asyncio.run(invites.consume(redis_client, code)) is True
    assert asyncio.run(invites.consume(redis_client, code)) is False

    asyncio.run(invites.refund(redis_client, code))
    assert asyncio.run(invites.consume(redis_client, code)) is True


def test_available_counts_only_unused_codes(redis_client):
    codes = mint(redis_client, 3)
    asyncio.run(invites.consume(redis_client, codes[0]))

    assert asyncio.run(invites.available(redis_client)) == 2


def test_the_used_set_is_capped(redis_client):
    """The used set is bookkeeping for the panel's count, so it is capped at
    MAX_USED_ENTRIES and the oldest entries are evicted; refund of an evicted
    code must still put it back in the unused set."""

    async def scenario():
        codes = await invites.mint(redis_client, invites.MAX_USED_ENTRIES + 25)
        for code in codes:
            assert await invites.consume(redis_client, code) is True
        used = await invites.used_count(redis_client)
        unused = await invites.available(redis_client)
        # codes[0] is the oldest, so it was evicted from the used set by now.
        await invites.refund(redis_client, codes[0])
        return used, unused, await invites.available(redis_client)

    used, unused, after_refund = asyncio.run(scenario())

    assert used == invites.MAX_USED_ENTRIES
    assert unused == 0
    assert after_refund == 1
