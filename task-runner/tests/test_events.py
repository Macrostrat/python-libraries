import asyncio
import json

from macrostrat.task_runner.events import output_events


class FakeAsyncRedis:
    def __init__(self, entries, stream_exists=True):
        self.entries = [
            (f"{i + 1}-0".encode(), {b"d": d.encode()}) for i, d in enumerate(entries)
        ]
        self.stream_exists = stream_exists

    async def exists(self, key):
        return self.stream_exists

    async def xread(self, streams, block=None, count=None):
        ((key, last),) = streams.items()
        after = [e for e in self.entries if _id(e[0]) > _id(last)]
        return [(key, after)] if after else []


def _id(entry_id) -> int:
    if isinstance(entry_id, bytes):
        entry_id = entry_id.decode()
    return int(entry_id.split("-")[0])


def collect(events):
    async def gather():
        return [e async for e in events]

    return asyncio.run(gather())


def _state(value):
    async def state():
        return value

    return state


async def _no_log():
    return None


def test_replays_from_last_event_id_then_ends():
    redis = FakeAsyncRedis(["a\r", "b", "c"])
    events = collect(
        output_events(
            redis, "r1", since="1-0", state=_state("succeeded"), archived_log=_no_log
        )
    )
    assert events[0] == f"id: 2-0\ndata: {json.dumps({'d': 'b'})}\n\n"
    assert events[1].startswith("id: 3-0\n")
    assert events[-1] == 'event: end\ndata: {"state": "succeeded"}\n\n'


def test_an_expired_stream_replays_the_archive():
    async def log():
        return "archived\r\n"

    events = collect(
        output_events(
            FakeAsyncRedis([], stream_exists=False),
            "r1",
            state=_state("cancelled"),
            archived_log=log,
        )
    )
    assert events == [
        f"id: 0-0\ndata: {json.dumps({'d': 'archived' + chr(13) + chr(10)})}\n\n",
        'event: end\ndata: {"state": "cancelled"}\n\n',
    ]
