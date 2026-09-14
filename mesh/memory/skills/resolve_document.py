"""
resolve_document's real body - thin wrapper around memory_index.py's
find_source_document(). DataPart-only, not advertised in SKILLS - see
mesh/memory/skills/ingest.py's own docstring for why an internal-only skill
stays out of the classify pool. This one is meant to be called by Analysis
Agent (mesh/analysis/skills/analyze.py) resolving a fuzzy document
reference, not classified from free text on Memory Agent's own card.

requester_id/is_owner scope the match the same way search_kb.py's do - see
memory_index.py's find_source_document() docstring.
"""
from typing import Any, Dict, Optional

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(query: str, requester_id: Optional[str] = None, is_owner: bool = False) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    source_filename = memory_index.find_source_document(query, requester_id=requester_id, is_owner=is_owner)
    if source_filename is None:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'source_filename': source_filename}
