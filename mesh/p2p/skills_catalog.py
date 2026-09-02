"""p2p's AgentSkill catalog - same pattern as every other agent's
skills_catalog.py."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'dispatch': {
        'name': 'Dispatch',
        'description': (
            "Discover a peer advertising the given model as a capability, via the "
            "self-hosted matchmaker, and send it a prompt to run instead of running "
            "it locally - Inference Router's own offload path when this machine is busy."
        ),
        'tags': ['p2p', 'offload'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
        'examples': ['Dispatch this prompt to a peer'],
    },
}


async def get_skills() -> List[AgentSkill]:
    return [AgentSkill(id=skill_id, **structure) for skill_id, structure in _STRUCTURE.items()]
