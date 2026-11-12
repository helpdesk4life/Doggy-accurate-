"""In-process registry of live WebSocket connections."""

import asyncio
import contextlib
from dataclasses import dataclass, field

from fastapi import WebSocket


@dataclass
class Connection:
    walker_id: str
    name: str
    websocket: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Who this walker could see on the last evaluation. Needed so that a walker who
    # moves out of range can be removed from the maps of everyone who was watching them.
    last_neighbors: set[str] = field(default_factory=set)


class Hub:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}

    def add(self, connection: Connection) -> None:
        self._connections[connection.walker_id] = connection

    def remove(self, walker_id: str) -> None:
        self._connections.pop(walker_id, None)

    def get(self, walker_id: str) -> Connection | None:
        return self._connections.get(walker_id)

    def name_of(self, walker_id: str) -> str:
        connection = self._connections.get(walker_id)
        return connection.name if connection else ""

    def all(self) -> list[Connection]:
        return list(self._connections.values())

    async def send(self, walker_id: str, payload: dict) -> bool:
        """Send to one walker. Returns False when the socket is gone."""
        connection = self._connections.get(walker_id)
        if connection is None:
            return False
        async with connection.lock:
            try:
                await connection.websocket.send_json(payload)
                return True
            except (RuntimeError, OSError):
                self.remove(walker_id)
                return False

    async def close(self, walker_id: str, code: int = 4401, reason: str = "") -> bool:
        """Close one walker's socket from the server side.

        Used when the account behind a live walk disappears, so the client stops
        broadcasting immediately instead of waiting for its token to expire.
        """
        connection = self._connections.pop(walker_id, None)
        if connection is None:
            return False
        async with connection.lock:
            with contextlib.suppress(RuntimeError, OSError):
                await connection.websocket.close(code=code, reason=reason)
        return True
