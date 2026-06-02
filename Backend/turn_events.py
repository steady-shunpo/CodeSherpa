"""
Registry of threading.Event objects used to pause run_agent_loop
(which runs in a thread) until an HTTP endpoint grants more turns.

Separate from the asyncio.Event registry used by _pause_for_intervention
because run_agent_loop is synchronous — it cannot await.
"""
import threading
from typing import NamedTuple

class TurnGrant(NamedTuple):
    extra_turns: int
    feedback: str | None  # optional message injected into agent context


_registry: dict[str, tuple[threading.Event, list[TurnGrant]]] = {}
_lock = threading.Lock()


def register(run_id: str) -> threading.Event:
    """Create and store an Event for this run. Call before launching the thread."""
    evt = threading.Event()
    with _lock:
        _registry[run_id] = (evt, [])
    return evt


def grant_turns(run_id: str, extra_turns: int, feedback: str | None) -> bool:
    """
    Called from the async HTTP handler (via asyncio.to_thread or directly).
    Stores the grant and sets the event so the blocked thread wakes up.
    Returns False if no event registered for this run_id.
    """
    with _lock:
        entry = _registry.get(run_id)
        if not entry:
            return False
        evt, grants = entry
        grants.append(TurnGrant(extra_turns=extra_turns, feedback=feedback))
        evt.set()
        return True


def wait_for_grant(run_id: str, timeout: float = 3600.0) -> TurnGrant | None:
    """
    Called from run_agent_loop (sync thread). Blocks until grant_turns() fires.
    Returns the TurnGrant or None on timeout.
    """
    with _lock:
        entry = _registry.get(run_id)
    if not entry:
        return None
    evt, grants = entry
    evt.wait(timeout=timeout)
    with _lock:
        if grants:
            evt.clear()
            return grants.pop(0)
    return None


def deregister(run_id: str) -> None:
    with _lock:
        _registry.pop(run_id, None)