"""Run the backend without Docker, backed by an in-memory Redis stand-in.

Handy on machines that have no Docker or no Redis server. The GEO commands served by
fakeredis are the same ones a real Redis serves, so the proximity logic behaves
identically; nothing is persisted between runs.

    python dev_server.py

Requires the dev dependencies: pip install -r requirements-dev.txt
"""

import fakeredis.aioredis
import uvicorn

from app.main import create_app

if __name__ == "__main__":
    app = create_app(redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
