"""Unbound API client (https://api.getunbound.ai) for ping and optional LLM use."""

from __future__ import annotations

import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["UNBOUND_API_KEY"],
            base_url="https://api.getunbound.ai/v1",
        )
    return _client


async def unbound_ping() -> str:
    """Call Unbound API with a minimal chat completion; returns model reply (expect 'UNBOUND_OK')."""
    client = _get_client()
    r = await client.chat.completions.create(
        model=os.getenv("UNBOUND_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": "Reply exactly: UNBOUND_OK"}],
        temperature=0.0,
    )
    return r.choices[0].message.content or ""
