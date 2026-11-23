"""A run's output as Server-Sent Events, for an API route to return.

Each `data` is `{"d": chunk}`, JSON because a raw carriage return would end an
SSE line, with the Redis stream entry as its `id`, so a reconnecting
`EventSource` resumes where it left off. A comment keeps the connection alive
through quiet stretches, and `event: end` carries the final state.
"""

import json
from typing import AsyncIterator, Awaitable, Callable

from .execute import FINAL_STATES
from .output import output_key
from .tail import tail_output

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def output_events(
    redis,
    run_id: str,
    *,
    state: Callable[[], Awaitable[str | None]],
    archived_log: Callable[[], Awaitable[str | None]],
    since: str = "0-0",
    disconnected: Callable[[], Awaitable[bool]] | None = None,
    final_states: tuple[str, ...] = FINAL_STATES,
) -> AsyncIterator[str]:
    """Replay the stream from `since` and follow it until the run ends.

    `redis` is an asyncio client. Once a finished run's stream has expired, the
    application's archived copy is replayed instead.
    """
    if await state() in final_states and not await redis.exists(output_key(run_id)):
        log = await archived_log()
        if log:
            yield _event("0-0", log)
        yield _end(await state())
        return

    async def finished() -> bool:
        return (await state()) in final_states

    async for item in tail_output(redis, run_id, since=since, finished=finished):
        if disconnected is not None and await disconnected():
            return
        if item is None:
            yield ": ping\n\n"
            continue
        yield _event(*item)
    yield _end(await state())


def _event(entry_id: str, chunk: str) -> str:
    return f"id: {entry_id}\ndata: {json.dumps({'d': chunk})}\n\n"


def _end(state: str | None) -> str:
    return f"event: end\ndata: {json.dumps({'state': state})}\n\n"
