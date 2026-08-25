"""
get_book_page's real body - thin wrapper around memory_index.py's
get_page(): one exact page's text from a document ingested via
ingest_book.py. DataPart-only, same reasoning as search_document_chunks.py's
own docstring - needs a filename AND a page number already in hand, not
something a free-text message would ever classify into.
"""
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(source_filename: str, page_number: int) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    # Same A2A/protobuf float-crossing cast search_document_chunks.py's
    # top_k already needs - page_number arrives as a double regardless of
    # what the caller sent.
    text = memory_index.get_page(source_filename, int(page_number))
    if text is None:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'text': text}
