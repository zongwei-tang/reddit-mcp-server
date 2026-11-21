from __future__ import annotations

import pytest

from reddit_mcp import http


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Never really sleep in tests; record requested waits instead."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(http, "sleep", fake_sleep)
    return waits
