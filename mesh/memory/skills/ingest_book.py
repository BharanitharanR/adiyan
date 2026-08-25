"""
ingest_book skill's real body - a thin wrapper around
mesh/memory/memory_index.py's MemoryIndex.ingest_document_by_page(). Same
DataPart-only, not-classify-able reasoning as ingest.py's own docstring -
this needs real document bytes, no prose can supply that.

Separate skill from ingest_document, not a mode flag on it - ingest_document
makes a document *searchable* (KB_COLLECTION_NAME, token/heading chunks);
this makes it *paginated* (KB_PAGES_COLLECTION_NAME, one point per page,
addressed by page_number, never searched). A caller wanting both calls both
skills - this one doesn't touch the knowledge base at all.
"""
import base64
import logging
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index

logger = logging.getLogger('MemoryIngestBook')


def run(content_b64: str, filename: str, username: str) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'ingested': False, 'num_pages': 0, 'available': False, 'error': None}

    content = base64.b64decode(content_b64)
    try:
        num_pages, source_filename = memory_index.ingest_document_by_page(content, filename, username)
    except Exception as e:
        logger.warning(f"Failed to ingest book {filename!r}: {e}")
        return {'ingested': False, 'num_pages': 0, 'available': True, 'error': str(e), 'source_filename': None}
    return {'ingested': True, 'num_pages': num_pages, 'available': True, 'error': None, 'source_filename': source_filename}
