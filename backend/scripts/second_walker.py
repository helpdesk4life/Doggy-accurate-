"""Drive a second walker against a running backend to exercise proximity alerts.

Usage:
    python scripts/second_walker.py [--url ws://127.0.0.1:8000/ws/walk]

The script walks a virtual walker from ~100 m away from the reference point
(Amsterdam Dam, matching `adb emu geo fix 4.8952 52.3702`) out to ~800 m and
prints every server frame it receives, so you can watch PROXIMITY_ALERT turn
into PROXIMITY_CLEAR.

The walker authenticates as a throwaway account derived from `--email`, which
defaults to the walker id. The account is created on first run.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from _auth import access_token, bearer

REFERENCE_LAT = 52.3702
REFERENCE_LON = 4.8952
METERS_PER_DEGREE_LAT = 111_320.0


def offset_lat(meters: float) -> float:
    return REFERENCE_LAT + meters / METERS_PER_DEGREE_LAT


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/walk")
    parser.add_argument("--walker-id", default="second-walker")
    parser.add_argument("--email", default=None, help="defaults to <walker-id>@example.com")
    parser.add_argument("--name", default="Buddy")
    args = parser.parse_args()

    import websockets

    email = args.email or f"{args.walker_id}@example.com"
    token = access_token(args.url, email)

    url = f"{args.url}?name={args.name}"
    async with websockets.connect(url, additional_headers=bearer(token)) as socket:

        async def pump() -> None:
            async for raw in socket:
                print("<-", json.dumps(json.loads(raw), indent=None), flush=True)

        reader = asyncio.create_task(pump())

        async def send(meters: float, label: str) -> None:
            frame = {
                "type": "location",
                "lat": offset_lat(meters),
                "lon": REFERENCE_LON,
                "accuracy": 5.0,
                "ts": 0,
            }
            print(f"-> {label} ({meters:.0f} m north)", flush=True)
            await socket.send(json.dumps(frame))

        await asyncio.sleep(0.5)
        await send(100, "inside radius")
        await asyncio.sleep(2.0)
        await send(800, "outside radius")
        await asyncio.sleep(2.0)

        reader.cancel()


if __name__ == "__main__":
    asyncio.run(main())
