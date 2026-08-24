"""Orchestrator Agent's AgentSkill catalog. One skill: take an incoming
message and a chat to reply to, figure out who should handle it, reply.
Primary caller is the whatsapp MCP server's webhook push (always a precise
DataPart call - see mesh/mcp/whatsapp/server.py), but kept A2A-compliant
with real examples for any future free-text caller too.

description/examples are Mongo-backed (config_sdk, see get_skills()) - the
actual prompt-shaping text a support person or the config dashboard would
plausibly want to tune. id/name/tags/input_modes/output_modes stay fixed
here - structural wiring (dispatch keys off `id`, nothing sensibly
"configurable" about a content-type string), not prompt content."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.lib import config_sdk
from mesh.orchestrator.constants import AGENT_ID

_DEFAULT_HANDLE_MESSAGE_DESCRIPTION = (
    'Route an incoming message to the right agent, get a response, and reply back to the given chat.'
)
_DEFAULT_HANDLE_MESSAGE_EXAMPLES = ['Handle this message and reply to the sender']

# {skill_id: {structural fields fixed in code}} - description/examples are
# looked up per skill_id at call time in get_skills() below.
_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'handle_message': {
        'name': 'Handle Message',
        'tags': ['orchestration', 'routing'],
        'input_modes': ['text/plain'],
        'output_modes': ['application/json'],
    },
}

_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'handle_message': {'description': _DEFAULT_HANDLE_MESSAGE_DESCRIPTION, 'examples': _DEFAULT_HANDLE_MESSAGE_EXAMPLES},
}


async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call, not cached module-level - config_sdk does its
    own short-TTL caching, so this stays cheap while still picking up a
    dashboard/WhatsApp edit to a skill's description within one cache
    window instead of only at process startup."""
    skills = []
    for skill_id, structure in _STRUCTURE.items():
        default = _DEFAULTS[skill_id]
        description = await config_sdk.get_constant(AGENT_ID, f'skill_{skill_id}_description', default['description'])
        examples = await config_sdk.get_constant(AGENT_ID, f'skill_{skill_id}_examples', default['examples'])
        skills.append(AgentSkill(id=skill_id, description=description, examples=examples, **structure))
    return skills
