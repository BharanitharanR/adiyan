"""Memory Agent's AgentSkill catalog - single source for server.py's card and
agent_executor.py's classifier prompt, same reasoning as
mesh/scheduler/skills_catalog.py.

Deliberately does NOT include ingest_document - see mesh/memory/skills/
ingest.py's own docstring for why. agent_executor.py still dispatches on
that skill_id; it just isn't advertised here, isn't in the Agent Registry,
and is never a candidate for classify()'s free-text matching.

description/examples are Mongo-backed (config_sdk) - same pattern as
mesh/orchestrator/skills_catalog.py's get_skills(), including the same
vertical-override capability. id/name/tags/input_modes/output_modes stay
fixed - dispatch wiring, not prompt content."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.lib import config_sdk
from mesh.memory.constants import AGENT_ID

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'recall_contact_memory': (
        "Look up what's known about one specific person from their past coaching "
        "conversations - actual back-and-forth chat history (moods, goals, updates "
        "someone told the coach directly), never a specific fact, ID number, or "
        "document's contents - that's search_knowledge_base's job even when the "
        "message says the word 'memory'. Only for a raw read-back of that history - "
        "'what do we know', 'what has X told us'. Not for a question that uses that "
        "history as input to reason, advise, or suggest something ('based on what "
        "you know about me, what should I focus on' / 'suggest a weekend plan for "
        "me') - that's a general-reasoning request, analyse_this's job, even though "
        "it also happens to reference personal history."
    ),
    'search_knowledge_base': (
        "Search documents the coach has uploaded (PDFs, photos of documents, notes, "
        "guides, ID cards, etc.) for information on a topic - global, not scoped to "
        "any one contact. Covers a direct factual question just as much as an "
        "explicit 'search my docs' request - if the answer could plausibly be sitting "
        "in an uploaded document, this is the skill, even if the message never says "
        "'search' or 'document' at all, and even if it's phrased as 'guess' or "
        "'estimate' rather than a direct question - the underlying fact (a salary "
        "figure, a date, an amount) is still just something to look up, not something "
        "that needs the whole document read and reasoned over."
    ),
    'share_knowledge_document': (
        'Find and send back the original uploaded document (not just a text snippet) '
        'that best matches a topic.'
    ),
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'recall_contact_memory': [
        "What do we know about how Priya's week has been going?",
        'Recall recent context for contact_name=sam_92',
        'What has Sam told us about his goals in past conversations?',
    ],
    'search_knowledge_base': [
        'Do I have anything about bicep curls?',
        'What does my nutrition guide say about protein intake?',
        'Search my uploaded docs for pricing information',
        'What is my aadhar number?',
        "What's Bharani's PAN number?",
        'What does the contract say about the cancellation policy?',
        "Guess Bharani's salary",
        'Estimate how much rent is mentioned in the lease',
    ],
    'share_knowledge_document': [
        'Share the bicep curls document with me',
        'Send me my nutrition guide PDF',
        'Can you share that knowledge with my users',
    ],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'recall_contact_memory': {
        'name': 'Recall Contact Memory', 'tags': ['memory', 'recall'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'search_knowledge_base': {
        'name': 'Search Knowledge Base', 'tags': ['memory', 'knowledge_base', 'search'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'share_knowledge_document': {
        'name': 'Share Knowledge Document', 'tags': ['memory', 'knowledge_base', 'share'],
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
