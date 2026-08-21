"""
recall_contact_memory's real body - a thin, honest wrapper around
mesh/memory/memory_index.py's MemoryIndex.retrieve(). No judgment here about
what the snippets mean or what to do with them - that's the caller's job
(Journal Agent). This just fetches.

The only place under mesh/ that reaches into memory_index.py's Qdrant/Ollama
stack directly - the whole reason Memory Agent exists as its own agent
instead of being a dependency baked into Journal Agent.
"""
from typing import Any, Dict, List

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(contact_name: str, query: str, top_k: int = 3) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        # Qdrant/Ollama unreachable - degrade, don't fail, same platform-wide
        # rule core/mcp_tools.py already documents for tool availability.
        return {'snippets': [], 'available': False}

    snippets: List[str] = memory_index.retrieve(query=query, contact_name=contact_name, top_k=top_k)
    return {'snippets': snippets, 'available': True}
