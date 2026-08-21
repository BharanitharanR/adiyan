"""
search_knowledge_base's real body - a thin, honest wrapper around
mesh/memory/memory_index.py's MemoryIndex.retrieve_knowledge_base(). Global,
not scoped to any one contact, unlike recall.py's recall_contact_memory -
see that method's own docstring.
"""
from typing import Any, Dict, List

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import KB_DEFAULT_TOP_K, get_memory_index


def run(query: str, top_k: int = KB_DEFAULT_TOP_K) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        # Qdrant/Ollama unreachable - degrade, don't fail, same rule
        # recall.py already follows.
        return {'snippets': [], 'available': False}

    snippets: List[str] = memory_index.retrieve_knowledge_base(query=query, top_k=top_k)
    return {'snippets': snippets, 'available': True}
