"""End-to-end test of the DogWalk backend running in Docker against real Redis."""
import asyncio, json, sys, uuid
import websockets

from _auth import access_token, bearer, call, mint_invite, reset_rate_limits

BASE = "ws://127.0.0.1:8000/ws/walk"
# Deliberately far from the Amsterdam Dam that the emulator and the manual
# scripts use. If a real app walker is parked on the reference point it becomes
# the nearest neighbour, which breaks the distance assertions and keeps
# PROXIMITY_CLEAR from ever firing. Rotterdam is ~57 km away, well outside the
# 500 m radius.
REF_LAT, REF_LON = 51.9225, 4.4792
MPD = 111_320.0

results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (("  -> " + detail) if detail else ""), flush=True)

def north(m):
    return REF_LAT + m / MPD

class Walker:
    def __init__(self, email, name):
        self.email, self.name = email, name
        self.frames = []
        self.ws = None
        self.task = None

    async def connect(self):
        token = access_token(BASE, self.email)
        self.ws = await websockets.connect(
            f"{BASE}?name={self.name}",
            additional_headers=bearer(token),
        )
        self.task = asyncio.create_task(self._pump())
        return await self.next_frame(3)

    async def _pump(self):
        try:
            async for raw in self.ws:
                self.frames.append(json.loads(raw))
        except Exception:
            pass

    async def send(self, lat, lon):
        await self.ws.send(json.dumps({"type": "location", "lat": lat, "lon": lon,
                                       "accuracy": 5.0, "ts": 0}))

    async def send_raw(self, text):
        await self.ws.send(text)

    async def next_frame(self, timeout=5, kind=None):
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for i, f in enumerate(self.frames):
                if kind is None or f.get("type") == kind:
                    return self.frames.pop(i)
            await asyncio.sleep(0.05)
        return None

    async def close(self):
        if self.task: self.task.cancel()
        if self.ws: await self.ws.close()

async def expect_rejected(url, headers=None):
    """Connect and report whether the server closed us with 4401."""
    try:
        async with websockets.connect(url, additional_headers=headers or {}) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
        return False, "connection stayed open"
    except websockets.exceptions.ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd else None
        return code == 4401, f"close code {code}"
    except Exception as exc:
        return False, repr(exc)

async def main():
    # The auth endpoints are throttled per hour, so a second run in the same
    # hour would fail on limits the first run spent rather than on anything
    # real. Clear them first; the helper refuses to touch a non-local Redis.
    cleared = reset_rate_limits()
    print(f"... cleared {cleared} rate-limit bucket(s)", flush=True)

    # Auth is the gate on everything else, so check it first.
    ok, detail = await expect_rejected(f"{BASE}?name=Nobody")
    check("socket without a token is rejected", ok, detail)

    ok, detail = await expect_rejected(f"{BASE}?name=Nobody", bearer("not-a-jwt"))
    check("socket with a bad token is rejected", ok, detail)

    a = Walker("e2e-a@example.com", "Noodle")
    b = Walker("e2e-b@example.com", "Buddy")

    w = await a.connect()
    check("welcome frame for walker A", w is not None and w.get("type") == "welcome", str(w))
    check("welcome carries radius 500", w is not None and w.get("radius_m") == 500.0, str(w))
    wb = await b.connect()
    check("welcome frame for walker B", wb is not None and wb.get("type") == "welcome", str(wb))

    # A at the Dam, B 100 m north -> both should see each other
    await a.send(REF_LAT, REF_LON)
    await asyncio.sleep(0.3)
    await b.send(north(100), REF_LON)

    fa = await a.next_frame(5, "PROXIMITY_ALERT")
    fb = await b.next_frame(5, "PROXIMITY_ALERT")
    check("A receives PROXIMITY_ALERT", fa is not None, str(fa))
    check("B receives PROXIMITY_ALERT", fb is not None, str(fb))
    if fa:
        d = fa["walkers"][0]["distance_m"]
        check("distance ~100 m", 95 <= d <= 105, f"{d} m")
        check("A sees B's name", fa["walkers"][0]["name"] == "Buddy", str(fa["walkers"][0]))
        check(
            "A's frame carries no coordinates",
            "lat" not in fa["walkers"][0] and "lon" not in fa["walkers"][0],
            str(fa["walkers"][0]),
        )
        check(
            "B is due north of A",
            fa["walkers"][0]["bearing_deg"] == 0.0,
            str(fa["walkers"][0]),
        )
    if fb:
        check("B sees A's name", fb["walkers"][0]["name"] == "Noodle", str(fb["walkers"][0]))
        check(
            "A is due south of B",
            fb["walkers"][0]["bearing_deg"] == 180.0,
            str(fb["walkers"][0]),
        )

    # B walks out to 800 m -> both clear
    await b.send(north(800), REF_LON)
    ca = await a.next_frame(5, "PROXIMITY_CLEAR")
    cb = await b.next_frame(5, "PROXIMITY_CLEAR")
    check("A receives PROXIMITY_CLEAR at 800 m", ca is not None, str(ca))
    check("B receives PROXIMITY_CLEAR at 800 m", cb is not None, str(cb))

    # B comes back -> alert again (re-entry must re-fire)
    await b.send(north(120), REF_LON)
    ra = await a.next_frame(5, "PROXIMITY_ALERT")
    check("re-entry fires PROXIMITY_ALERT again", ra is not None, str(ra))

    # validation
    await a.send_raw(json.dumps({"type": "location", "lat": 999, "lon": 0}))
    e1 = await a.next_frame(5, "error")
    check("out-of-range lat rejected", e1 is not None, str(e1))
    await a.send_raw("{not json")
    e2 = await a.next_frame(5, "error")
    check("malformed JSON rejected", e2 is not None, str(e2))

    # sweeper: A goes silent, B keeps walking. A must expire after TTL.
    print("... waiting for the 30 s TTL sweeper (A silent, B still sending)", flush=True)
    expired = False
    for _ in range(12):
        await asyncio.sleep(5)
        await b.send(north(120), REF_LON)
        f = await b.next_frame(2, "PROXIMITY_CLEAR")
        if f is not None:
            expired = True
            break
    check("silent walker expires and watcher is cleared", expired)

    await a.close()
    await b.close()

    # --- auth surface added in the security pass -------------------------
    status, _ = call(BASE, "POST", "/auth/password-reset/request", {"email": "nobody@example.com"})
    check("reset request for an unknown address is 202", status == 202, f"HTTP {status}")

    throwaway = "e2e-delete@example.com"
    token = access_token(BASE, throwaway)

    status, _ = call(
        BASE, "DELETE", "/auth/account", {"password": "wrong-password"}, token=token
    )
    check("delete account rejects a wrong password", status == 401, f"HTTP {status}")

    status, _ = call(
        BASE, "DELETE", "/auth/account", {"password": "script-walker-password"}, token=token
    )
    check("delete account succeeds", status == 204, f"HTTP {status}")

    status, _ = call(
        BASE, "POST", "/auth/login",
        {"email": throwaway, "password": "script-walker-password"},
    )
    check("deleted account can no longer log in", status == 401, f"HTTP {status}")

    # --- invite gate ------------------------------------------------------
    # The property that matters is not "a code is required" but that the answer
    # does not depend on whether the address exists. e2e-a is registered by now,
    # so it is the taken case; the other address is fresh every run, so it is
    # genuinely never-seen and the check stays repeatable.
    taken = {"email": "e2e-a@example.com", "password": "script-walker-password"}
    free = {
        "email": f"e2e-invite-{uuid.uuid4().hex[:8]}@example.com",
        "password": "script-walker-password",
    }

    status, body = call(BASE, "POST", "/auth/register", taken)
    check("register without a code is refused", status == 403, f"HTTP {status}")

    taken_no_code = (status, body)
    free_no_code = call(BASE, "POST", "/auth/register", free)
    check(
        "taken and free addresses are indistinguishable without a code",
        taken_no_code == free_no_code,
        f"{taken_no_code} vs {free_no_code}",
    )

    status, _ = call(BASE, "POST", "/auth/register", {**free, "invite_code": "AAAAA-AAAAA"})
    check("register with an unknown code is refused", status == 403, f"HTTP {status}")

    status, body = call(BASE, "POST", "/auth/register", {**free, "invite_code": mint_invite()})
    check("register with a minted code succeeds", status == 201, f"HTTP {status}")

    # Leave no account behind, so the next run starts from the same place.
    if status == 201:
        call(
            BASE, "DELETE", "/auth/account",
            {"password": free["password"]}, token=body["access_token"],
        )

    print("\n=== SUMMARY ===", flush=True)
    bad = [r for r in results if not r[1]]
    print(f"{len(results) - len(bad)}/{len(results)} passed", flush=True)
    for name, _, detail in bad:
        print("  FAILED:", name, detail, flush=True)
    return 0 if not bad else 1

sys.exit(asyncio.run(main()))
