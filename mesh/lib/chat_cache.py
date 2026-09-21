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
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from pydantic import BaseModel, Field

from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_seed_config

# Shared platform prompt, same pseudo agent_id convention as
# skill_router.py/tool_resolution.py/vision.py - relevance filtering is the
# same decision regardless of which agent's conversation this cache belongs to.
_SHARED_AGENT_ID = 'chat_cache'
_agent = AdiyanAgent(_SHARED_AGENT_ID)
_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})

logger = logging.getLogger(__name__)

DEFAULT_MODE = 'count'
DEFAULT_MAX_TURNS = 10
DEFAULT_WINDOW_MINUTES = 30.0

# Written-at-capacity ceiling regardless of mode - see this module's own
# docstring on why.
_HARD_CAP_TURNS = 200

# Bounds a single turn's text when building format_recent_turns()'s relevance-
# classification prompt below - CHAT_CACHE_MAX_TURNS bounds turn *count*, not
# each turn's own length, and up to 10 turns get concatenated into one prompt.
# Same "an LLM prompt input needs its own size bound" reasoning as
# mesh/analysis/skills/analyze.py's OBSERVATION_CHAR_CAP, much smaller here
# since this caps one turn of chat, not a whole tool observation.
_TURN_CHAR_CAP = 500

_lock = threading.Lock()
_turns_by_contact: Dict[str, Deque[Dict[str, Any]]] = {}


def _mode() -> str:
    return os.environ.get('CHAT_CACHE_MODE', DEFAULT_MODE)


def _max_turns() -> int:
    return int(os.environ.get('CHAT_CACHE_MAX_TURNS', str(DEFAULT_MAX_TURNS)))


def _window_minutes() -> float:
    return float(os.environ.get('CHAT_CACHE_WINDOW_MINUTES', str(DEFAULT_WINDOW_MINUTES)))


def _cache_key(contact_name: str, vertical_id: Optional[str]) -> str:
    """Same composite-key scoping mesh/memory/mem0_backend.py's own
    _scoped_user_id uses, for the same reason - confirmed live: a contact
    who talked to a demo yoga-teacher vertical and asked for its Zoom link,
    then separately messaged a completely different real vertical (an
    executive-coaching assistant, via its own unrelated summon phrase),
    got that Zoom link repeated back to them out of nowhere - this cache's
    own get_recent_turns(), keyed on contact_name alone, handed the yoga
    exchange to format_recent_turns() as "recent history" for a conversation
    that had nothing to do with it, and the model treated it as legitimate
    prior context. vertical_id=None (a genuinely cross-business flow) falls
    back to the old, unscoped key rather than inventing a fake bucket."""
    return f'{vertical_id}::{contact_name}' if vertical_id else contact_name


def remember_turn(contact_name: str, user_text: str, reply_text: str, vertical_id: Optional[str] = None) -> None:
    """Best-effort, never raises - a failure here must never break the reply
    that already went out, same tolerance every other best-effort write in
    this mesh gets (e.g. mem0_backend.remember()).

    vertical_id must match whatever get_recent_turns()/format_recent_turns()
    is later called with for this same contact, or this turn will never be
    found - see _cache_key's own docstring for why this exists."""
    key = _cache_key(contact_name, vertical_id)
    try:
        with _lock:
            turns = _turns_by_contact.setdefault(key, deque(maxlen=_HARD_CAP_TURNS))
            turns.append({'user_text': user_text, 'reply_text': reply_text, 'timestamp': time.time()})
    except Exception:
        pass


def get_recent_turns(contact_name: str, vertical_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Oldest-first list of {'user_text', 'reply_text', 'timestamp'}, bounded
    by whichever mode CHAT_CACHE_MODE currently selects.

    vertical_id: see remember_turn's own docstring - must match the write
    side for this contact, or nothing will come back."""
    key = _cache_key(contact_name, vertical_id)
    with _lock:
        turns = list(_turns_by_contact.get(key, ()))

    if _mode() == 'time':
        cutoff = time.time() - (_window_minutes() * 60)
        return [t for t in turns if t['timestamp'] >= cutoff]
    return turns[-_max_turns():]


class _RelevantTurns(BaseModel):
    relevant_indices: List[int] = Field(
        default_factory=list,
        description=(
            'Indices (from the numbered list) of turns that are relevant '
            'context for the new message, oldest first. Empty if none of '
            'the history is relevant.'
        ),
    )


async def format_recent_turns(
    contact_name: str, new_message: str, cfg: Dict[str, Any], vertical_id: Optional[str] = None,
) -> Optional[str]:
    """Turns get_recent_turns()'s raw window into an LLM-ready context block,
    filtered down to only the turns actually relevant to new_message - not the
    whole window verbatim. Confirmed live: "I really enjoy trekking in the
    Himalayas" -> ack -> "What gear should I pack for it?" got "which
    activity?" back instead of resolving "it" to trekking, because nothing
    read get_recent_turns()'s output before this. A flat per-turn relevance
    classification against new_message, keeping the surviving turns in their
    original chronological order, is enough for that - no graph between turn
    pairs is needed (an unrelated turn like "what is 1+1"/"15" sitting between
    two relevant turns must drop out without disturbing the order of the ones
    kept).

    Returns None when there is nothing to prepend - no history at all, or the
    classifier judged nothing in it relevant - so the caller can skip
    prepending, same "empty means don't bother" convention as
    mesh/analysis/skills/analyze.py's _merge_document_list/
    search_within_document.

    cfg (model/temperature) comes from the caller, resolved via config_sdk on
    its end - this module is a shared library, not tied to one agent_id, same
    reasoning analyze.py's _decide_next_step/_compact take cfg as a parameter
    rather than fetching it themselves."""
    turns = get_recent_turns(contact_name, vertical_id)
    if not turns:
        return None

    def _cap(text: str, cap: int) -> str:
        return text if len(text) <= cap else text[:cap] + '...'

    numbered = '\n\n'.join(
        f'[{i}] User: {_cap(t["user_text"], _TURN_CHAR_CAP)}\n'
        f'    Reply: {_cap(t["reply_text"], _TURN_CHAR_CAP)}'
        for i, t in enumerate(turns)
    )
    seeded = _seeded('chat_cache_relevance_prompt_template')
    template = await config_sdk.get_constant(
        _SHARED_AGENT_ID, 'chat_cache_relevance_prompt_template', seeded['value'], description=seeded['description'],
    )
    fmt_kwargs = dict(numbered=numbered, new_message=new_message)
    try:
        prompt = template.format(**fmt_kwargs)
    except Exception:
        prompt = seeded['value'].format(**fmt_kwargs)
    try:
        result = await _agent.ask(
            prompt, stage='relevance_filter', model=cfg['model'], temperature=cfg['temperature'], schema=_RelevantTurns,
        )
        keep = sorted({i for i in result.relevant_indices if 0 <= i < len(turns)})
    except Exception as e:
        logger.warning(f'format_recent_turns: relevance filter failed, using full window: {e}')
        keep = list(range(len(turns)))

    if not keep:
        return None

    return '\n\n'.join(
        f'User: {turns[i]["user_text"]}\nReply: {turns[i]["reply_text"]}'
        for i in keep
    )


def clear(contact_name: str, vertical_id: Optional[str] = None) -> None:
    """Not currently called anywhere - here for symmetry/testing, same as
    any cache needs a way to be emptied."""
    with _lock:
        _turns_by_contact.pop(_cache_key(contact_name, vertical_id), None)
