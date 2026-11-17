"""Account requests: a stranger asks, an operator approves.

Two properties matter here, and neither is the CRUD.

**The public endpoint must not answer "does this address have an account?"** That
is the enumeration oracle the invite gate exists to close, and a request
endpoint is a second door onto it. So it answers 202 for a free address, a taken
one, a duplicate and a full backlog alike.

**An approval must not hand out a credential.** The account is created with a
password nobody keeps, so the only way in is the reset flow — which is the
account choosing its own password. The approval email says so and carries no
code.
"""

import asyncio
import re

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app import account_requests, admin, users
from app.main import create_app

ADMIN_EMAIL = "operator@example.com"
ADMIN_PASSWORD = "operator-password"
PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password"
EMAIL = "alice@example.com"
OTHER = "bob@example.com"


class RecordingSender:
    """Stands in for the email sender, and can be made to fail."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail = False

    async def send(self, to: str, subject: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("provider is down")
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def client(redis_client):
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=False, invite_required=False)
    ) as test_client:
        yield test_client


@pytest.fixture
def limited_client(redis_client):
    with TestClient(
        create_app(redis_client=redis_client, rate_limit=True, invite_required=False)
    ) as test_client:
        yield test_client


@pytest.fixture
def sender(client):
    recorder = RecordingSender()
    client.app.state.email_sender = recorder
    return recorder


@pytest.fixture
def operator(redis_client):
    return asyncio.run(admin.create_admin(redis_client, ADMIN_EMAIL, ADMIN_PASSWORD))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def admin_token(client) -> str:
    response = client.post(
        "/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def request_account(client, email: str = EMAIL):
    return client.post("/auth/account-request", json={"email": email})


def register(client, email: str = EMAIL) -> dict:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def pending(redis_client) -> list[dict]:
    return asyncio.run(account_requests.pending(redis_client))


def sign_in(client) -> None:
    response = client.post(
        "/admin/ui/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def approve(client, request_id: str):
    return client.post(
        f"/admin/requests/{request_id}/approve", headers=bearer(admin_token(client))
    )


def _keys(value) -> set[str]:
    """Every key name anywhere in a JSON body."""
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _keys(item)
        return found
    return set()


# --- the public endpoint ----------------------------------------------------


def test_a_request_is_accepted(client, redis_client):
    response = request_account(client)

    assert response.status_code == 202
    assert [record["email"] for record in pending(redis_client)] == [EMAIL]


def test_the_answer_does_not_reveal_whether_the_address_has_an_account(client):
    """The whole point: a taken address and a free one look identical."""
    register(client, EMAIL)

    taken = request_account(client, EMAIL)
    free = request_account(client, OTHER)

    assert taken.status_code == free.status_code == 202
    assert taken.text == free.text


def test_a_duplicate_request_is_not_queued_twice(client, redis_client):
    assert request_account(client).status_code == 202
    assert request_account(client).status_code == 202

    assert asyncio.run(account_requests.count(redis_client)) == 1


def test_a_request_for_an_existing_account_is_not_queued(client, redis_client):
    register(client, EMAIL)

    assert request_account(client).status_code == 202
    assert asyncio.run(account_requests.count(redis_client)) == 0


def test_the_backlog_is_capped(client, redis_client):
    for index in range(account_requests.MAX_PENDING):
        asyncio.run(account_requests.create(redis_client, f"walker{index}@example.com"))

    response = request_account(client, "one-too-many@example.com")

    assert response.status_code == 202
    assert asyncio.run(account_requests.count(redis_client)) == account_requests.MAX_PENDING


def test_a_malformed_address_is_rejected(client):
    """A 422 here is about the shape of the address, not about who is registered."""
    assert client.post("/auth/account-request", json={"email": "nope"}).status_code == 422


def test_the_endpoint_is_rate_limited(limited_client, frozen_window):
    for _ in range(5):
        assert request_account(limited_client).status_code == 202

    assert request_account(limited_client).status_code == 429


# --- the admin API ----------------------------------------------------------


def test_requests_need_an_admin_token(client, operator):
    assert client.get("/admin/requests").status_code == 401


def test_a_walker_token_cannot_list_requests(client, operator):
    session = register(client)

    response = client.get("/admin/requests", headers=bearer(session["access_token"]))

    assert response.status_code == 401


def test_listing_is_newest_first(client, operator):
    request_account(client, EMAIL)
    request_account(client, OTHER)

    body = client.get("/admin/requests", headers=bearer(admin_token(client))).json()

    assert [record["email"] for record in body["requests"]] == [OTHER, EMAIL]
    assert body["total"] == 2


def test_approving_creates_the_account_and_emails_the_requester(
    client, operator, sender, redis_client
):
    request_account(client)
    request_id = pending(redis_client)[0]["request_id"]

    response = approve(client, request_id)

    assert response.status_code == 201, response.text
    assert response.json()["email"] == EMAIL
    assert response.json()["email_sent"] is True

    account = asyncio.run(users.get_by_email(redis_client, EMAIL))
    assert account is not None
    assert users.must_change_password(account)

    assert [message["to"] for message in sender.sent] == [EMAIL]
    assert pending(redis_client) == []


def test_the_approval_email_carries_no_code(client, operator, sender, redis_client):
    """A code in the mail would be a credential with a fifteen-minute life."""
    request_account(client)
    approve(client, pending(redis_client)[0]["request_id"])

    assert re.search(r"\b\d{6}\b", sender.sent[0]["body"]) is None
    assert "Forgot password" in sender.sent[0]["body"]


def test_an_approved_account_has_no_password_anyone_knows(client, operator, sender, redis_client):
    request_account(client)
    response = approve(client, pending(redis_client)[0]["request_id"])

    # Nothing in the response is a credential.
    assert "password" not in response.json()
    assert "temporary_password" not in response.json()

    # And the account cannot be signed into with anything guessable.
    assert client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401


def test_the_requester_sets_their_own_password_through_the_reset_flow(
    client, operator, sender, redis_client
):
    """The journey the approval email describes, end to end."""
    request_account(client)
    approve(client, pending(redis_client)[0]["request_id"])

    assert client.post("/auth/password-reset/request", json={"email": EMAIL}).status_code == 202
    code = re.search(r"\b(\d{6})\b", sender.sent[-1]["body"]).group(1)

    confirmed = client.post(
        "/auth/password-reset/confirm",
        json={"email": EMAIL, "code": code, "password": NEW_PASSWORD},
    )
    assert confirmed.status_code == 204

    signed_in = client.post("/auth/login", json={"email": EMAIL, "password": NEW_PASSWORD})
    assert signed_in.status_code == 200, signed_in.text
    # A real session, not a ticket: the reset cleared the handed-over flag.
    assert signed_in.json()["access_token"]
    assert signed_in.json()["must_change_password"] is False


def test_approving_an_unknown_request_is_404(client, operator):
    assert approve(client, "nope").status_code == 404


def test_approving_twice_is_404_the_second_time(client, operator, sender, redis_client):
    request_account(client)
    request_id = pending(redis_client)[0]["request_id"]

    assert approve(client, request_id).status_code == 201
    assert approve(client, request_id).status_code == 404


def test_approving_an_address_that_registered_meanwhile_is_409(
    client, operator, sender, redis_client
):
    request_account(client)
    register(client, EMAIL)

    response = approve(client, pending(redis_client)[0]["request_id"])

    assert response.status_code == 409
    # The request was moot, so it does not sit in the queue forever.
    assert pending(redis_client) == []


def test_a_failed_send_still_approves(client, operator, sender, redis_client):
    """The account exists either way; "Forgot password" does not need the notice."""
    request_account(client)
    sender.fail = True

    response = approve(client, pending(redis_client)[0]["request_id"])

    assert response.status_code == 201
    assert response.json()["email_sent"] is False
    assert asyncio.run(users.get_by_email(redis_client, EMAIL)) is not None


def test_rejecting_creates_nothing(client, operator, redis_client):
    request_account(client)
    request_id = pending(redis_client)[0]["request_id"]

    response = client.delete(
        f"/admin/requests/{request_id}", headers=bearer(admin_token(client))
    )

    assert response.status_code == 204
    assert pending(redis_client) == []
    assert asyncio.run(users.get_by_email(redis_client, EMAIL)) is None


def test_rejecting_an_unknown_request_is_404(client, operator):
    response = client.delete("/admin/requests/nope", headers=bearer(admin_token(client)))

    assert response.status_code == 404


def test_approval_is_audited(client, operator, sender, redis_client):
    request_account(client)
    approve(client, pending(redis_client)[0]["request_id"])

    entries = asyncio.run(admin.recent(redis_client, 20))
    entry = next(item for item in entries if item["action"] == "approve_request")

    assert entry["detail"] == EMAIL
    assert entry["target"]


def test_rejection_is_audited(client, operator, redis_client):
    request_account(client)
    request_id = pending(redis_client)[0]["request_id"]
    client.delete(f"/admin/requests/{request_id}", headers=bearer(admin_token(client)))

    entries = asyncio.run(admin.recent(redis_client, 20))

    assert any(
        item["action"] == "reject_request" and item["detail"] == EMAIL for item in entries
    )


def test_no_request_response_carries_a_position(client, operator, sender, redis_client):
    request_account(client)
    token = admin_token(client)
    listed = client.get("/admin/requests", headers=bearer(token))
    approved = approve(client, pending(redis_client)[0]["request_id"])

    forbidden = {"lat", "lon", "distance_m", "bearing_deg"}
    assert not (_keys(listed.json()) & forbidden)
    assert not (_keys(approved.json()) & forbidden)


# --- the panel --------------------------------------------------------------


def test_the_requests_page_needs_a_cookie(client, operator):
    response = client.get("/admin/ui/requests", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ui/login"


def test_the_requests_page_lists_a_request(client, operator):
    request_account(client)
    sign_in(client)

    response = client.get("/admin/ui/requests")

    assert response.status_code == 200
    assert EMAIL in response.text


def test_the_panel_approves(client, operator, sender, redis_client):
    request_account(client)
    sign_in(client)
    request_id = pending(redis_client)[0]["request_id"]

    response = client.post(
        f"/admin/ui/requests/{request_id}/approve", follow_redirects=False
    )

    assert response.status_code == 303
    assert asyncio.run(users.get_by_email(redis_client, EMAIL)) is not None
    assert pending(redis_client) == []


def test_the_panel_rejects(client, operator, redis_client):
    request_account(client)
    sign_in(client)
    request_id = pending(redis_client)[0]["request_id"]

    response = client.post(
        f"/admin/ui/requests/{request_id}/reject", follow_redirects=False
    )

    assert response.status_code == 303
    assert pending(redis_client) == []
    assert asyncio.run(users.get_by_email(redis_client, EMAIL)) is None


def test_the_dashboard_counts_pending_requests(client, operator):
    request_account(client)
    sign_in(client)

    response = client.get("/admin/ui")

    assert '<div class="value">1</div><div class="label">account requests</div>' in response.text
