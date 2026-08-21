"""
share_knowledge_document's real body - finds the knowledge-base document
that best matches a query and returns its original bytes, not just a text
snippet (that's search_kb.py's job). A distinct skill from
search_knowledge_base because the two produce fundamentally different
replies on Orchestrator's side: a snippet becomes ordinary text; this
becomes an actual file delivered over WhatsApp. Orchestrator doesn't special
-case this skill_id to know the difference - see
mesh/orchestrator/skills/handle_message.py's own docstring: any result
carrying content_b64 is understood as "deliver a file," regardless of which
skill produced it.
"""
from typing import Any, Dict

from mesh.memory.constants import OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import get_memory_index


def run(query: str) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    source_filename = memory_index.find_source_document(query)
    if source_filename is None:
        return {'found': False, 'available': True}

    document = memory_index.get_document(source_filename)
    if document is None:
        # Matched a chunk, but the raw file itself isn't on file (ingested
        # before raw storage existed, or its stored copy went missing) - a
        # real, distinct outcome from "nothing matched at all."
        return {'found': False, 'available': True}

    return {'found': True, 'available': True, **document}
