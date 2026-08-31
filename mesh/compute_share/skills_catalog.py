"""compute_share's AgentSkill catalog - same pattern as every other
agent's skills_catalog.py."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'run_inference': {
        'name': 'Run Inference',
        'description': 'Run a single LLM completion for a fully-built prompt and return the text. No document, memory, or conversation access.',
        'tags': ['compute-share', 'inference'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
        'examples': ['Run this prompt: summarize the following in one sentence...'],
    },
    'announce_peer': {
        'name': 'Announce Peer',
        'description': 'Register that a peer is willing to take offloaded inference work.',
        'tags': ['compute-share'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
        'examples': ['Announce peer http://192.168.1.42:8460 with model qwen3:8b-16k'],
    },
    'offload': {
        'name': 'Offload',
        'description': "Send a prompt to a known peer's Adiyan instead of running it locally, when this machine is busy.",
        'tags': ['compute-share'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
        'examples': ['Offload this prompt to a peer'],
    },
}


async def get_skills() -> List[AgentSkill]:
    return [AgentSkill(id=skill_id, **structure) for skill_id, structure in _STRUCTURE.items()]
