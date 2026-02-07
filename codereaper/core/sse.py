"""Minimal SSEChannel stub.

The full SSE implementation was removed along with the FastAPI REST layer.
This stub keeps the type annotation ``channel: SSEChannel | None = None``
valid in scanner and verifier services.  The MCP server always passes
``channel=None``, so the send/close methods are never called.
"""

from __future__ import annotations


class SSEChannel:
    """No-op stub for the removed SSE channel."""

    async def send(self, event: str, data: object = None) -> None:  # noqa: D401
        """No-op: silently discard the event."""

    async def close(self) -> None:
        """No-op: nothing to close."""
