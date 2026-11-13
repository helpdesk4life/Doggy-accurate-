"""Fixtures shared by more than one test module.

The rate limiter buckets on ``int(time.time()) // window_seconds``, so its
counters reset on a wall-clock boundary. A test that sends exactly
``limit.requests + 1`` requests is therefore a coin flip: if the boundary falls
between the last allowed request and the one that should be blocked, the counter
starts again from zero and the request goes through.

That is the documented tradeoff of a fixed window — a burst can straddle a
boundary and get two windows' worth — not a bug. But it turns those tests into
failures that mean nothing, which is worse than not having them. ``frozen_window``
pins the clock so the window cannot move underneath the test.
"""

import pytest

from app import ratelimit


class _FrozenClock:
    """Stands in for the ``time`` module inside ``app.ratelimit``."""

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def frozen_window(monkeypatch):
    """Pin the limiter's clock. Call ``.advance(n)`` to cross a window boundary."""
    clock = _FrozenClock(1_000_000.0)
    monkeypatch.setattr(ratelimit, "time", clock)
    return clock
