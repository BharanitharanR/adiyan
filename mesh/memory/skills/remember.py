"""
remember_interaction's real body - thin wrapper around
mesh/memory/mem0_backend.py's remember(). DataPart-only, not advertised in
SKILLS - see mesh/memory/skills/ingest.py's own docstring for why an
internal-only skill stays out of the classify pool. This one is meant to be
called by Orchestrator (mesh/orchestrator/skills/handle_message.py) once a
real conversation exchange has already happened, not classified from free
text on Memory Agent's own card - there's no user-facing request that
should ever mean "please remember this."
"""
from typing import Any, Dict

from mesh.memory import mem0_backend


def run(contact_name: str, user_text: str, reply_text: str) -> Dict[str, Any]:
    if not mem0_backend.is_available():
        return {'remembered': False, 'available': False}
    mem0_backend.remember(contact_name=contact_name, user_text=user_text, reply_text=reply_text)
    return {'remembered': True, 'available': True}
