import asyncio
from typing import Dict

# Global registry: run_id (str) → asyncio.Queue
_queues: Dict[str, asyncio.Queue] = {}
_loop: asyncio.AbstractEventLoop = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def get_or_create_queue(run_id: str) -> asyncio.Queue:
    if run_id not in _queues:
        _queues[run_id] = asyncio.Queue()
    return _queues[run_id]


def get_queue(run_id: str) -> asyncio.Queue | None:
    return _queues.get(run_id)


def drop_queue(run_id: str):
    _queues.pop(run_id, None)


# Called from stage functions (sync context, inside to_thread)


def publish_token(run_id: str, token: str, loop: asyncio.AbstractEventLoop):
    queue = get_queue(run_id)
    if queue is None:
        return
    loop.call_soon_threadsafe(queue.put_nowait, token)


# Sentinel — signals the SSE endpoint that the stage is done
STREAM_DONE = "__STREAM_DONE__"