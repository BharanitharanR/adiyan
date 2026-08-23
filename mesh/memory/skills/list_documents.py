"""
list_documents' real body - thin wrapper around memory_index.py's
list_documents(). DataPart-only, not advertised in SKILLS - see
mesh/memory/skills/ingest.py's own docstring for why. Called by Analysis
Agent's ReAct loop when it needs to see what documents exist at all,
instead of relying on a search query happening to match one.
"""
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run() -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'documents': [], 'available': False}
    return {'documents': memory_index.list_documents(), 'available': True}
