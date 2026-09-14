"""
search_knowledge_base's real body - a thin, honest wrapper around
mesh/memory/memory_index.py's MemoryIndex.retrieve_knowledge_base().
Scoped per-requester (see that method's own docstring), not global - unlike
the un-scoped design this replaced, this now behaves the same way
recall.py's recall_contact_memory already did (scoped to whoever's asking).

requester_id/is_owner never come from a caller-supplied param the way
query/top_k do - see mesh/memory/agent_executor.py's own comment on why
these two are injected there from the caller's verified token instead, and
mesh/analysis/skills/analyze.py's own comment on why its tool closures pass
the real end-user's identity through explicitly rather than letting it get
silently replaced by Analysis Agent's own service identity.
"""
from typing import Any, Dict, List, Optional

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import KB_DEFAULT_TOP_K, get_memory_index


def run(
    query: str, top_k: int = KB_DEFAULT_TOP_K,
    requester_id: Optional[str] = None, is_owner: bool = False,
) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        # Qdrant/Ollama unreachable - degrade, don't fail, same rule
        # recall.py already follows.
        return {'snippets': [], 'available': False}

    snippets: List[str] = memory_index.retrieve_knowledge_base(
        query=query, top_k=top_k, requester_id=requester_id, is_owner=is_owner,
    )
    return {'snippets': snippets, 'available': True}
