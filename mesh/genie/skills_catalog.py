"""Genie's AgentSkill catalog - same single-source-of-truth pattern
as every other agent's skills_catalog.py (see mesh/journal/skills_catalog.py
for the smallest real one this was copied from).

description/examples are Mongo-backed via config_sdk, so they're editable
from the config dashboard without a restart - the orchestrator picks up an
edited description the next time it routes a message, since this whole
function is rebuilt on every call rather than cached at import time.

This is the one place the orchestrator actually reads to decide "does this
conversation belong to Genie, or to someone else?" - the
description below IS the routing logic, in plain English, not a
regex or a keyword list."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.genie.constants import AGENT_ID
from mesh.lib import config_sdk

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'socratic_nudge': 'Called only by the Daily Practice tablet app: one short Socratic hint (a guiding question) for a practice question, never the answer. Not for chat messages.',
    'listen_and_check': "Called only by the Daily Practice tablet app: transcribes a child's recording (reading aloud, or explaining how they solved a problem) and checks how well they did. Not for chat messages.",
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'socratic_nudge': ['(structured call from the Daily Practice app only)'],
    'listen_and_check': ['(structured call from the Daily Practice app only)'],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'socratic_nudge': {
        'name': 'Socratic Nudge', 'tags': ['genie', 'daily-practice'],
        'input_modes': ['application/json'], 'output_modes': ['application/json'],
    },
    'listen_and_check': {
        'name': 'Listen and Check', 'tags': ['genie', 'daily-practice', 'whisper'],
        'input_modes': ['application/json'], 'output_modes': ['application/json'],
    },
}


async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call - config_sdk's own short-TTL cache keeps this
    cheap while still picking up a dashboard edit within one cache window,
    not only at process startup."""
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
