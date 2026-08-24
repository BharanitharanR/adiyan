"""Journal Agent's AgentSkill catalog - same single-source-of-truth pattern
as every other agent's skills_catalog.py.

description/examples are Mongo-backed (config_sdk) - same pattern as
mesh/memory/skills_catalog.py's get_skills(), including the same
vertical-override capability. id/name/tags/input_modes/output_modes stay
fixed - dispatch wiring, not prompt content."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.journal.constants import AGENT_ID
from mesh.lib import config_sdk

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'craft_reflection_prompt': (
        "Craft a tailored journaling question for one person, personalized from "
        "what's known about them if anything is - otherwise a thoughtful general one."
    ),
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'craft_reflection_prompt': [
        'Craft a reflection prompt for sam_92, themed around work stress',
        'Give me a journaling question for tonight',
        'Remind me every night to journal',
        'Help me journal tonight',
    ],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'craft_reflection_prompt': {
        'name': 'Craft Reflection Prompt', 'tags': ['journal', 'reflection'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
}


async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call - config_sdk's own short-TTL cache keeps this
    cheap while still picking up a dashboard/WhatsApp edit (or a vertical
    activation) within one cache window, not only at process startup."""
    skills = []
    for skill_id, structure in _STRUCTURE.items():
        description = await config_sdk.get_constant(
            AGENT_ID, f'skill_{skill_id}_description', _DEFAULT_DESCRIPTIONS[skill_id],
        )
        examples = await config_sdk.get_constant(
            AGENT_ID, f'skill_{skill_id}_examples', _DEFAULT_EXAMPLES[skill_id],
        )
        skills.append(AgentSkill(id=skill_id, description=description, examples=examples, **structure))
    return skills
