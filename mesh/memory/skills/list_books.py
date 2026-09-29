"""
list_books' real body - thin wrapper around memory_index.py's
list_page_ingested_books(). DataPart-only, not advertised in SKILLS - same
reasoning as list_documents.py's own docstring, and deliberately separate
from it: list_documents() only ever sees kb_documents (chunk-ingested via
ingest_document); a page-ingested book (ingest_book()) never gets a row
there at all (see list_page_ingested_books()'s own docstring).

vertical_id is the primary scope - a business's own shared library, so one
of ABJ's subscribers sees ABJ's own books, never btwin's or the owner's
personal uploads. requester_id (auto-injected from the caller's own token
claims, see agent_executor.py's _SCOPED_SKILLS) is an additional prefix,
not a replacement - it still surfaces a person's own personal uploads
alongside whatever vertical library they're asking from, since existing
books ingested before vertical-scoped storage existed are keyed by
individual username, not vertical_id.
"""
from typing import Any, Dict, List, Optional

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def _display_title(source_filename: str) -> str:
    import re
    basename = source_filename.split('/', 1)[-1]
    basename = re.sub(r'\.[A-Za-z0-9]+$', '', basename)
    return basename.replace('_', ' ').replace('-', ' ').strip()


def run(
    vertical_id: Optional[str] = None, requester_id: Optional[str] = None, is_owner: bool = False,
) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'books': [], 'available': False}

    prefixes: List[str] = [p for p in (vertical_id, requester_id) if p]
    # The owner sees every book on file, same as list_documents()'s own
    # is_owner behavior - no prefix filter at all rather than one that
    # would exclude their own platform-wide view.
    source_filenames = memory_index.list_page_ingested_books(prefixes=None if is_owner else prefixes)
    return {'books': [_display_title(s) for s in source_filenames], 'available': True}
