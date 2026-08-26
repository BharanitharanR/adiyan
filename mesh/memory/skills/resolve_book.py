"""
resolve_book's real body - thin wrapper around memory_index.py's
find_book_by_reference(). DataPart-only, not advertised in skills_catalog.py
(same reasoning as resolve_document.py's own docstring) - this is meant to
be called by Orchestrator resolving a free-text book reference
(mesh/orchestrator/skills/handle_message.py's _start_book_reading()) before
ever calling adiyan_reader's start_reading, never classified from free text
on Memory Agent's own card.

Deliberately separate from resolve_document.py: that one searches
kb_documents (chunk-ingested via ingest_document), which has zero
visibility into a page-ingested book (ingest_book/ingest_document_by_page
never writes a kb_documents row) - see find_book_by_reference()'s own
docstring for how this was found live.
"""
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(query: str) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    source_filename = memory_index.find_book_by_reference(query)
    if source_filename is None:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'source_filename': source_filename}
