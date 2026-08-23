"""
Shared short-term/chat-memory cache - a per-contact rolling window of recent
exchanges, distinct from mesh/memory/mem0_backend.py's long-term semantic
memory (mem0's retrieve() finds whatever's relevant by meaning; this finds
whatever happened most recently, verbatim, for coreference/follow-up
continuity - "what about tomorrow instead" only makes sense against the
actual last few turns, not a semantic search hit).

A plain in-process cache, not persisted anywhere - restarting any process
that imports this module starts it empty again, deliberately (this is a
cache, not a store; mem0_backend.py already owns durable conversation
history). Orchestrator is the first, and for now only, writer/reader
(remember_turn() is called from handle_message.py's same should_remember
branch mem0 remembering already uses) - once a compaction engine exists to
merge get_recent_turns()'s output with the latest prompt before handing off
to child agents, that's a separate, later piece built on top of this, not
part of this module.

Bounding is toggleable, not fixed to one policy - CHAT_CACHE_MODE ('count',
the default, or 'time') picks which of CHAT_CACHE_MAX_TURNS /
CHAT_CACHE_WINDOW_MINUTES applies at read time, so flipping the toggle takes
effect immediately with no separate write-time logic per mode. Turns are
still capped at write time by _HARD_CAP_TURNS regardless of mode, purely so
a contact that never stops chatting can't grow a single deque unboundedly
between reads.
"""
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List

DEFAULT_MODE = 'count'
DEFAULT_MAX_TURNS = 10
DEFAULT_WINDOW_MINUTES = 30.0

# Written-at-capacity ceiling regardless of mode - see this module's own
# docstring on why.
_HARD_CAP_TURNS = 200

_lock = threading.Lock()
_turns_by_contact: Dict[str, Deque[Dict[str, Any]]] = {}


def _mode() -> str:
    return os.environ.get('CHAT_CACHE_MODE', DEFAULT_MODE)


def _max_turns() -> int:
    return int(os.environ.get('CHAT_CACHE_MAX_TURNS', str(DEFAULT_MAX_TURNS)))


def _window_minutes() -> float:
    return float(os.environ.get('CHAT_CACHE_WINDOW_MINUTES', str(DEFAULT_WINDOW_MINUTES)))


def remember_turn(contact_name: str, user_text: str, reply_text: str) -> None:
    """Best-effort, never raises - a failure here must never break the reply
    that already went out, same tolerance every other best-effort write in
    this mesh gets (e.g. mem0_backend.remember())."""
    try:
        with _lock:
            turns = _turns_by_contact.setdefault(contact_name, deque(maxlen=_HARD_CAP_TURNS))
            turns.append({'user_text': user_text, 'reply_text': reply_text, 'timestamp': time.time()})
    except Exception:
        pass


def get_recent_turns(contact_name: str) -> List[Dict[str, Any]]:
    """Oldest-first list of {'user_text', 'reply_text', 'timestamp'}, bounded
    by whichever mode CHAT_CACHE_MODE currently selects."""
    with _lock:
        turns = list(_turns_by_contact.get(contact_name, ()))

    if _mode() == 'time':
        cutoff = time.time() - (_window_minutes() * 60)
        return [t for t in turns if t['timestamp'] >= cutoff]
    return turns[-_max_turns():]


def clear(contact_name: str) -> None:
    """Not currently called anywhere - here for symmetry/testing, same as
    any cache needs a way to be emptied."""
    with _lock:
        _turns_by_contact.pop(contact_name, None)
