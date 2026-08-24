"""Analysis Agent's AgentSkill catalog - single source for server.py's card
and agent_executor.py's classifier prompt, same reasoning as every other
agent's skills_catalog.py.

Renamed from analyze_document to analyse_this - the id and description used
to describe a document-only capability (split a document into windows,
analyze each once, combine). That's no longer what this agent does: it's a
ReAct loop (mesh/analysis/skills/analyze.py) that can pull from documents,
recall conversation memory, and consult other registered agents, deciding
for itself which of those it actually needs for a given request - and it's
also Orchestrator's fallback for anything nothing else classifies (see
mesh/orchestrator/skills/handle_message.py), not something reachable only
via an explicit "analyze X" phrasing. The old narrower description would
have kept the classifier blind to everything this agent can now actually do.

Deliberately still not a fixed verb list ("analyze," "review," "compare")
in the examples below - the same reasoning the prior draft of this catalog
already settled on holds even more now that the scope is broader: staying
example-diverse is what avoids a legitimate request getting missed because
it didn't happen to resemble the one flavor of example on file (this
collided unpredictably once already - recall_contact_memory vs
search_knowledge_base, both plausible for "check my memory and find my
aadhar number").

description/examples are Mongo-backed (config_sdk) - same pattern as
mesh/orchestrator/skills_catalog.py's get_skills(), including the same
vertical-override capability: a business-vertical agent (marketplace, to
be introduced) can give this skill its own description/examples for its
own deployment. id/name/tags/input_modes/output_modes stay fixed - dispatch
wiring, not prompt content."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.analysis.constants import AGENT_ID
from mesh.lib import config_sdk

_DEFAULT_DESCRIPTION = (
    'General-purpose reasoning: anything that needs real thinking, not a single '
    'stored fact - analyzing or reviewing an entire document, answering an '
    'open-ended question using what is known about the person asking, or a '
    'request nothing more specific fits. Not for a single-fact lookup against a '
    "document ('what is X', 'do I have Y') - that's search_knowledge_base's job."
)

_DEFAULT_EXAMPLES = [
    'Find the spelling mistakes in the AI native payment validation presentation',
    'Analyze this presentation and share the mistakes you find',
    'Review the contract for inconsistencies',
    'Summarize the nutrition guide',
    'Translate this document to Tamil',
    'List every action item mentioned in this document',
    "What's the overall tone of this document?",
    'What should I look for backpacking in monsoon season?',
    'Prescribe me a good protein-rich menu for tonight',
    'Based on what you know about me, what should I focus on this week?',
]

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'analyse_this': {
        'name': 'Analyse This',
        'tags': ['analysis', 'reasoning', 'documents', 'memory'],
        'input_modes': ['text/plain'],
        'output_modes': ['application/json'],
    },
}

_DEFAULTS: Dict[str, Dict[str, Any]] = {
    'analyse_this': {'description': _DEFAULT_DESCRIPTION, 'examples': _DEFAULT_EXAMPLES},
}


async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call - config_sdk's own short-TTL cache keeps this
    cheap while still picking up a dashboard/WhatsApp edit (or a vertical
    activation) within one cache window, not only at process startup."""
    skills = []
    for skill_id, structure in _STRUCTURE.items():
        default = _DEFAULTS[skill_id]
        description = await config_sdk.get_constant(AGENT_ID, f'skill_{skill_id}_description', default['description'])
        examples = await config_sdk.get_constant(AGENT_ID, f'skill_{skill_id}_examples', default['examples'])
        skills.append(AgentSkill(id=skill_id, description=description, examples=examples, **structure))
    return skills
