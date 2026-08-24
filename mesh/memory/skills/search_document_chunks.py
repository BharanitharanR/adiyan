"""
search_document_chunks's real body - thin wrapper around memory_index.py's
search_within_document(): semantic search scoped to one already-known
document, not the whole knowledge base and not a blind full-text dump.
DataPart-only, not advertised in SKILLS - same reasoning as
get_document_text.py's own docstring: this needs a filename AND a query
already in hand, which only makes sense mid-investigation from Analysis
Agent (mesh/analysis/skills/analyze.py), not something a free-text message
to Memory Agent's own card would ever classify into on its own.
"""
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import DOC_SEARCH_DEFAULT_TOP_K, get_memory_index


def run(source_filename: str, query: str, top_k: int = DOC_SEARCH_DEFAULT_TOP_K) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    # A2A's protobuf Struct has no integer type, only double - top_k arrives
    # as a float across the wire regardless of what the caller sent.
    # Confirmed live once already this session for recall.py's own top_k -
    # cast explicitly rather than let LlamaIndex's retriever choke on it.
    chunks = memory_index.search_within_document(source_filename, query, top_k=int(top_k))
    if not chunks:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'chunks': chunks}
