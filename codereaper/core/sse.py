"""Server-Sent Events helpers for streaming long-running operations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from starlette.responses import StreamingResponse


def sse_event(event: str, data: dict[str, Any]) -> str:
    """Format a single SSE frame.

    Example output::

        event: scan_progress
        data: {"percent": 42, "message": "Exploring..."}

    """
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_response(generator: AsyncGenerator[str, None]) -> StreamingResponse:
    """Wrap an async string generator in an SSE-compatible StreamingResponse."""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class SSEChannel:
    """Pub/sub channel for broadcasting SSE events to multiple listeners.

    The scanner / verifier service pushes events into the channel,
    and the SSE endpoint consumes them.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False

    async def send(self, event: str, data: dict[str, Any]) -> None:
        """Publish an event to the channel."""
        if not self._closed:
            await self._queue.put(sse_event(event, data))

    async def close(self) -> None:
        """Signal end-of-stream."""
        self._closed = True
        await self._queue.put(None)

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """Yield SSE frames until the channel is closed."""
        while True:
            frame = await self._queue.get()
            if frame is None:
                break
            yield frame
