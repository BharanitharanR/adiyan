"""
ingest_document skill's real body - a thin wrapper around
mesh/memory/memory_index.py's MemoryIndex.ingest_document() ("parse with
Docling and chunk" - not PDF-specific, despite the module below still living
at mesh/memory/skills/ingest.py).

Deliberately not reachable via free text (see this skill's absence from
mesh/memory/skills_catalog.py's SKILLS, and mesh/orchestrator/router.py's
_url_by_agent_id comment) - it needs real document bytes no prose can
supply. Its only caller is mesh/orchestrator/skills/handle_message.py's
kb_pending branch, via a direct call_agent(skill_id='ingest_document', ...).

Unlike ingest_document() itself, this catches the parse/extraction failure
ingest_document()'s own docstring says to expect, rather than letting it
propagate - mesh/memory/agent_executor.py has no try/except around a skill
dispatch (recall.py and search_kb.py never raise), and an uncaught exception
here would crash the whole task with an opaque error instead of a message
the coach can actually act on ("that PDF was a scanned image with no OCR
text"), the same class of bug already fixed once for delivery failures in
handle_message.py.
"""
import base64
import logging
from typing import Any, Dict, Optional

from mesh.lib.errors import describe_exception
from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index

logger = logging.getLogger('MemoryIngest')


def run(
    content_b64: str, filename: str, timestamp: str, username: str, mimetype: Optional[str] = None,
    owner_identity: Optional[str] = None, visibility: str = 'private', do_ocr: bool = True,
) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'ingested': False, 'chunks': 0, 'available': False, 'error': None}

    content = base64.b64decode(content_b64)
    try:
        chunks, source_filename = memory_index.ingest_document(
            content, filename, timestamp, username, mimetype=mimetype,
            owner_identity=owner_identity, visibility=visibility, do_ocr=do_ocr,
        )
    except ValueError as e:
        # A deliberate, user-actionable message memory_index.py itself
        # raised (e.g. "scanned image with no OCR text") - genuinely worth
        # reporting to the coach over WhatsApp, per this module's own
        # docstring on why this is caught here rather than swallowed.
        logger.warning(f"Failed to ingest {filename!r}: {e}")
        return {'ingested': False, 'chunks': 0, 'available': True, 'error': str(e), 'source_filename': None}
    except Exception as e:
        # Anything else is an internal failure (storage, network, a bug) -
        # never handed to handle_message.py as `error` text, which puts it
        # straight into a WhatsApp reply verbatim. Confirmed live: a Qdrant
        # 404 for a missing collection reached a real customer as raw JSON
        # ("Unexpected Response: 404 (Not Found)... Collection ... doesn't
        # exist!") - the same class of raw-error leak already fixed once
        # for delivery failures elsewhere in this mesh, just not here yet.
        # error=None here, same as the `memory_index is None` branch above,
        # so the caller falls back to its own generic message.
        logger.error(f"Failed to ingest {filename!r}: {describe_exception(e)}")
        return {'ingested': False, 'chunks': 0, 'available': True, 'error': None, 'source_filename': None}
    return {'ingested': True, 'chunks': chunks, 'available': True, 'error': None, 'source_filename': source_filename}
