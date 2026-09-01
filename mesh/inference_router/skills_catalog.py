"""Inference Router's AgentSkill catalog - same single-source-of-truth pattern
as every other agent's skills_catalog.py (see mesh/journal/skills_catalog.py
for the smallest real one this was copied from).

description/examples are Mongo-backed via config_sdk, so they're editable
from the config dashboard without a restart - the orchestrator picks up an
edited description the next time it routes a message, since this whole
function is rebuilt on every call rather than cached at import time.

This is the one place the orchestrator actually reads to decide "does this
conversation belong to Inference Router, or to someone else?" - the
description below IS the routing logic, in plain English, not a
regex or a keyword list."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.inference_router.constants import AGENT_ID
from mesh.lib import config_sdk

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'complete': (
        "Run a single one-off LLM completion on behalf of a calling agent, deciding whether to run it "
        "locally or offload to a peer's spare compute if this machine is busy and the caller opted in. "
        "Internal platform plumbing (mesh/lib/agent_sdk.py's ask() is the only real caller) - never something "
        "a human message should route to directly."
    ),
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'complete': ['Run this prompt and return the completion'],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'complete': {
        'name': 'Complete', 'tags': ['internal', 'inference'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
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
