"""
get_document_text's real body - thin wrapper around memory_index.py's
get_document_text(), an exact-key lookup returning the FULL reconstructed
text of one document (every stored chunk, concatenated in order), not a
handful of similarity-matched snippets. DataPart-only, not advertised - see
mesh/memory/skills/ingest.py's own docstring for why. Called by Analysis
Agent (mesh/analysis/skills/analyze.py) once it already knows exactly which
document it means, either from resolve_document or because the caller
supplied source_filename directly.

requester_id/is_owner scope this exact-key lookup too, not just the search
that might have found source_filename in the first place - see
memory_index.py's get_document_text() own docstring on why knowing a
filename must not be enough on its own to read it.
"""
from typing import Any, Dict, Optional

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(source_filename: str, requester_id: Optional[str] = None, is_owner: bool = False) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    text = memory_index.get_document_text(source_filename, requester_id=requester_id, is_owner=is_owner)
    if text is None:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'text': text}
