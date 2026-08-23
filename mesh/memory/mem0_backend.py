"""
Conversation memory, backed by mem0ai instead of a hand-rolled
extraction/consolidation pipeline - replaces the old MemoryIndex.insert()/
retrieve() pair (removed from mesh/memory/memory_index.py; it had zero
callers anywhere in this repo, so there was no data to migrate).

Confirmed live before this was wired in, not assumed: mem0ai (v2.0.18) runs
fully local against Ollama (LLM + embedder) and Adiyan's own Qdrant
instance, with zero network calls when MEM0_TELEMETRY=false - verified via
its own source (mem0/memory/telemetry.py: MEM0_TELEMETRY=false sets
self.posthog = None, and capture_event() returns immediately when
self.posthog is None, before touching the network at all). The env var is
set here, before mem0 is imported, so it's never left to whatever the
process's shell environment happens to have.

Confirmed live, and the actual reason retrieve() below re-ranks by recency
instead of trusting Mem0's own similarity ranking as-is: a corrected fact
("actually my favorite color is crimson, not teal") does NOT delete or
supersede the old one in this version - both survive as separate memories,
and pure cosine similarity ranked the STALE fact above the correcting one
(0.781 vs 0.725) against the query "what is my favorite color", because
the shorter original statement embeds slightly closer to a short query
than the longer correction sentence does. A naive top-result retrieval
would confidently return the wrong, superseded answer - the same
"confidently wrong from bad ranking/empty context" failure category
already hit once this session (the PPTX/Docling OCR gap). Re-ranking by
recency (the retrieval-scoring approach from the Generative Agents paper)
fixes this without needing Mem0 to actually delete anything: a recent
correction still wins even when it isn't the closest embedding match.
"""
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

os.environ.setdefault('MEM0_TELEMETRY', 'false')

from mem0 import Memory  # noqa: E402 - must follow the MEM0_TELEMETRY env var above

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL  # noqa: E402

logger = logging.getLogger('Mem0Backend')

# Separate collection from mesh/memory/memory_index.py's adiyan_knowledge_base
# - different engine (mem0ai's own point schema), different content (per-
# contact conversation facts, not uploaded documents), no reason to share.
COLLECTION_NAME = 'adiyan_conversation_memory'
EMBED_MODEL = 'nomic-embed-text'
EMBED_DIMS = 768  # confirmed against Adiyan's existing Qdrant collections, not assumed
LLM_MODEL = 'qwen3:8b-16k'

# Exponential recency decay, in days - halves a memory's effective retrieval
# weight roughly every 14 days. Tunable: lower favors very recent context
# more aggressively, higher keeps older facts competitive longer. Not
# validated against real usage yet - a reasonable starting point, not a
# claimed-optimal constant.
RECENCY_HALF_LIFE_DAYS = 14.0

DEFAULT_TOP_K = 5

_memory: Optional[Memory] = None


def _get_memory() -> Optional[Memory]:
    """Cached factory - None (not an error) if Ollama/Qdrant aren't
    reachable, so callers can treat conversation memory as an optional
    enhancement rather than a hard dependency, same as
    mesh/memory/memory_index.py's get_memory_index()."""
    global _memory
    if _memory is not None:
        return _memory
    try:
        parsed = urlparse(QDRANT_URL)
        _memory = Memory.from_config({
            'llm': {
                'provider': 'ollama',
                'config': {'model': LLM_MODEL, 'ollama_base_url': OLLAMA_URL, 'temperature': 0.2},
            },
            'embedder': {
                'provider': 'ollama',
                'config': {'model': EMBED_MODEL, 'ollama_base_url': OLLAMA_URL, 'embedding_dims': EMBED_DIMS},
            },
            'vector_store': {
                'provider': 'qdrant',
                'config': {
                    'host': parsed.hostname,
                    'port': parsed.port,
                    'collection_name': COLLECTION_NAME,
                    'embedding_model_dims': EMBED_DIMS,
                },
            },
        })
    except Exception as e:
        logger.warning(f"⚠️  Conversation memory unavailable ({e}) - continuing without it")
    return _memory


def is_available() -> bool:
    """True if Ollama/Qdrant are reachable and conversation memory can
    actually be used - callers (recall.py) use this to report availability
    honestly rather than inferring it from an empty result list, which is
    also the correct outcome for a real, successful "nothing relevant
    found" search."""
    return _get_memory() is not None


def remember(contact_name: str, user_text: str, reply_text: str) -> None:
    """Stores one real conversation exchange as {user turn, assistant turn}
    - Mem0's own extraction decides what's actually salient from that pair,
    not this function. Best-effort: a failure here never blocks a WhatsApp
    reply that's already been delivered (see
    mesh/orchestrator/skills/handle_message.py's own call site, which calls
    this only after delivery, wrapped so its own failure can't affect what
    the user already received)."""
    memory = _get_memory()
    if memory is None:
        return
    try:
        memory.add(
            [
                {'role': 'user', 'content': user_text},
                {'role': 'assistant', 'content': reply_text},
            ],
            user_id=contact_name,
        )
    except Exception as e:
        logger.warning(f"Failed to store conversation memory for {contact_name!r}: {e}")


def _recency_weight(timestamp_str: str) -> float:
    """0.0-1.0, decaying by RECENCY_HALF_LIFE_DAYS. 0.5 (a neutral, neither-
    favored-nor-penalized weight) if the timestamp is missing/unparseable -
    should not happen given Mem0 always sets created_at/updated_at itself,
    but this is user-facing ranking, not something to let crash on a
    format surprise."""
    if not timestamp_str:
        return 0.5
    try:
        ts = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return 0.5
    age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    return 0.5 ** (max(age_days, 0) / RECENCY_HALF_LIFE_DAYS)


def retrieve(contact_name: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[str]:
    """Up to top_k past interaction snippets for this contact, ranked by
    similarity combined with recency - not Mem0's own raw similarity
    ranking. See this module's own docstring for the confirmed-live reason
    that matters: a corrected fact doesn't reliably outrank the stale one
    it corrected, on similarity alone."""
    memory = _get_memory()
    if memory is None:
        return []
    try:
        # A wider net than top_k - recency re-ranking can promote a memory
        # that wasn't even in Mem0's own raw top-k similarity results.
        result = memory.search(query, filters={'user_id': contact_name}, top_k=max(top_k * 3, 10))
    except Exception as e:
        logger.warning(f"Failed to search conversation memory for {contact_name!r}: {e}")
        return []

    scored = [
        (r.get('score', 0.0) * _recency_weight(r.get('updated_at') or r.get('created_at') or ''), r['memory'])
        for r in result.get('results', [])
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [text for _, text in scored[:top_k]]
