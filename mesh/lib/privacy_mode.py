"""
Per-contact LLM privacy mode. Default ("regular"): ask() may use the Claude
CLI backend. "Incognito": every LLM call made on that contact's behalf stays
on local Ollama until they switch back.

Same two-sided contract as persona_hook/memory_hook: bootstrap.py's executor
wrapper resolves the flag once per incoming A2A request from the verified
token's identity and vertical and sets CURRENT_INCOGNITO (a ContextVar, not a
threaded parameter); ask() reads it. Orchestrator re-sets it itself once
rules_engine.check() has resolved which vertical applies, for the same reason
it re-sets the persona.

The switch is a fixed-phrase command (parse_command), not an LLM judgement:
deciding whether someone asked for privacy must never itself depend on
sending their message to a hosted model, and a misread should never flip it.

State is one Mongo document per vertical::identity in `contact_privacy`, so it
survives restarts and is visible to every agent process. Lookups are cached
for a few seconds per process. A failed lookup resolves to incognito
(fail closed): if the flag can't be read, nothing leaves the machine.

Scope: this controls which LLM answers. It does not stop chat history, mem0
memory, or llm_calls.log from recording the exchange.
"""
import asyncio
import contextvars
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from mesh.lib import mongo_shared

logger = logging.getLogger('PrivacyMode')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
COLLECTION = 'contact_privacy'

CURRENT_INCOGNITO: contextvars.ContextVar[bool] = contextvars.ContextVar('adiyan_incognito', default=False)

# Deliberately says nothing about which model answers or where it runs - see
# mesh/lib/model_confidentiality.py. What incognito does is internal.
INCOGNITO_ON_REPLY = 'Incognito is on. Your chats are handled privately on this system until you say "use regular".'
INCOGNITO_OFF_REPLY = "Back to regular mode."

_CACHE_TTL_SECONDS = 10.0
_cache: Dict[str, Tuple[bool, float]] = {}


def _collection():
    return mongo_shared.database()[COLLECTION]


def _doc_id(identity_key: str, vertical_id: Optional[str]) -> str:
    return f'{vertical_id}::{identity_key}' if vertical_id else identity_key


def _read(doc_id: str) -> bool:
    doc = _collection().find_one({'_id': doc_id}, {'incognito': 1})
    return bool(doc and doc.get('incognito'))


async def is_incognito(identity_key: Optional[str], vertical_id: Optional[str] = None) -> bool:
    if not identity_key:
        return False
    doc_id = _doc_id(identity_key, vertical_id)
    cached = _cache.get(doc_id)
    if cached and time.monotonic() - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    try:
        value = await asyncio.to_thread(_read, doc_id)
    except Exception as e:
        logger.warning(f'privacy flag lookup failed for {doc_id!r}, treating as incognito: {e}')
        return True
    _cache[doc_id] = (value, time.monotonic())
    return value


async def set_incognito(identity_key: str, vertical_id: Optional[str], on: bool) -> None:
    doc_id = _doc_id(identity_key, vertical_id)

    def _write() -> None:
        _collection().update_one(
            {'_id': doc_id},
            {'$set': {'incognito': on, 'updated_at': datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )

    await asyncio.to_thread(_write)
    _cache[doc_id] = (on, time.monotonic())


_QUESTION_START = re.compile(r'^(what|why|how|when|who|which|is|are|does|do|can|could|will|would)\b')
_REGULAR_WORDS = {'regular', 'normal', 'standard'}
_OFF_WORDS = {'exit', 'leave', 'stop', 'out', 'of', 'off', 'end', 'disable', 'quit', 'cancel'}
_SWITCH_WORDS = {'use', 'go', 'switch', 'back', 'return', 'resume', 'mode', 'turn'}
# Everything else a command message may contain. A message holding any word
# outside this set and the keywords ("use the regular voice", "I went
# incognito in chrome") is conversation, not a mode switch.
_FILLER_WORDS = _OFF_WORDS | _SWITCH_WORDS | {
    'please', 'now', 'the', 'a', 'an', 'to', 'into', 'in', 'on', 'be', 'get', 'enter', 'adiyan', 'abj',
    'agent', 'assistant', 'bot', 'let', 'lets', 'us', 'i', 'want', 'like', 'just', 'again', 'you', 'me',
    'it', 'chat', 'chats', 'my', 'this',
}
_MAX_WORDS = 10


def parse_command(text: str) -> Optional[bool]:
    """True = switch to incognito, False = switch back to regular, None = not
    a privacy command. Only short, non-question messages made up entirely of
    command words count - a sentence that merely mentions incognito, or asks
    for a "regular" voice, is not a request."""
    lowered = (text or '').lower().strip()
    if not lowered or '?' in lowered:
        return None
    normalized = re.sub(r'[^\w\s]', ' ', lowered)
    words = normalized.split()
    if not words or len(words) > _MAX_WORDS or _QUESTION_START.match(normalized.strip()):
        return None
    has_incognito = 'incognito' in words
    has_regular = any(w in _REGULAR_WORDS for w in words)
    if has_incognito == has_regular:
        return None
    if any(w not in _FILLER_WORDS and w != 'incognito' and w not in _REGULAR_WORDS for w in words):
        return None
    if has_incognito:
        return False if any(w in _OFF_WORDS for w in words) else True
    return False if any(w in _SWITCH_WORDS for w in words) else None
