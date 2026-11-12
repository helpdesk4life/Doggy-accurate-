"""Hold a virtual walker at a fixed distance from the reference point.

Usage:
    python scripts/hold_walker.py --meters 60 --name Buddy

Unlike `second_walker.py`, which sweeps in and out once, this keeps re-sending
the same fix every few seconds so the walker stays alive past `WALKER_TTL_SECONDS`.
That makes it the script to use when you want a stable second dog on the map
while you poke at the Android UI.

The reference point is the Amsterdam Dam, matching `adb emu geo fix 4.8952 52.3702`.
Pick `--meters` relative to the app's warning distance to test the alert gate:
60 m fires a notification at the default 100 m, 120 m stays silent.

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
    parser.add_argument("--meters", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=5.0)
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

        frame = {
            "type": "location",
            "lat": offset_lat(args.meters),
            "lon": REFERENCE_LON,
            "accuracy": 5.0,
            "ts": 0,
        }
        print(f"-> holding {args.name} at {args.meters:.0f} m north", flush=True)

        try:
            while True:
                await socket.send(json.dumps(frame))
                await asyncio.sleep(args.interval)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            reader.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
