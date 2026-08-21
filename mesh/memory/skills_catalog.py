"""Memory Agent's AgentSkill catalog - single source for server.py's card and
agent_executor.py's classifier prompt, same reasoning as
mesh/scheduler/skills_catalog.py.

Deliberately does NOT include ingest_document - see mesh/memory/skills/
ingest.py's own docstring for why. agent_executor.py still dispatches on
that skill_id; it just isn't advertised here, isn't in the Agent Registry,
and is never a candidate for classify()'s free-text matching."""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='recall_contact_memory',
        name='Recall Contact Memory',
        description=(
            "Look up what's known about one specific person from their past coaching "
            "conversations - actual back-and-forth chat history (moods, goals, updates "
            "someone told the coach directly), never a specific fact, ID number, or "
            "document's contents - that's search_knowledge_base's job even when the "
            "message says the word 'memory'."
        ),
        tags=['memory', 'recall'],
        examples=[
            "What do we know about how Priya's week has been going?",
            'Recall recent context for contact_name=sam_92',
            'What has Sam told us about his goals in past conversations?',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='search_knowledge_base',
        name='Search Knowledge Base',
        description=(
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
        tags=['memory', 'knowledge_base', 'search'],
        examples=[
            'Do I have anything about bicep curls?',
            'What does my nutrition guide say about protein intake?',
            'Search my uploaded docs for pricing information',
            'What is my aadhar number?',
            "What's Bharani's PAN number?",
            'What does the contract say about the cancellation policy?',
            "Guess Bharani's salary",
            'Estimate how much rent is mentioned in the lease',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
    AgentSkill(
        id='share_knowledge_document',
        name='Share Knowledge Document',
        description='Find and send back the original uploaded document (not just a text snippet) that best matches a topic.',
        tags=['memory', 'knowledge_base', 'share'],
        examples=[
            'Share the bicep curls document with me',
            'Send me my nutrition guide PDF',
            'Can you share that knowledge with my users',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
