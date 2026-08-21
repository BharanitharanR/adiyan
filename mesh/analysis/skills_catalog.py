"""Analysis Agent's AgentSkill catalog - single source for server.py's card
and agent_executor.py's classifier prompt, same reasoning as every other
agent's skills_catalog.py.

The real classifier boundary against Memory Agent's search_knowledge_base
is NOT a verb category (analyze/review/critique) - it's whether answering
needs the document's ENTIRE content or just whichever few chunks best
match a query. Proofreading, translating, comparing, extracting a list of
every X, outlining, judging overall tone - all of these need the whole
document, none of them are single-fact lookups. Examples below span that
full range deliberately, not just "find what's wrong with this" - an
earlier, narrower draft of this catalog only had error-finding examples,
which would have made the classifier blind to anything else in this
category (translate, compare, list every clause, outline this deck).
A fuzzy "which one is this really asking" boundary already collided
unpredictably once before (recall_contact_memory vs search_knowledge_base,
both plausible for "check my memory and find my aadhar number") - staying
deliberately broad within "needs the whole document" is meant to avoid a
narrower version of that same mistake, where a legitimate whole-document
request gets missed because it didn't happen to resemble the one flavor of
example on file."""
from a2a.types import AgentSkill

SKILLS = [
    AgentSkill(
        id='analyze_document',
        name='Analyze Document',
        description=(
            "Any request that needs an entire uploaded document's content, not a single "
            "matching snippet - reviewing, critiquing, summarizing, comparing, "
            "translating, outlining, extracting every instance of something, or judging "
            "the document as a whole in any way. Not for a single-fact lookup "
            "('what is X', 'do I have Y') even against the same document - that's "
            "search_knowledge_base's job."
        ),
        tags=['analysis', 'documents', 'synthesis'],
        examples=[
            'Find the spelling mistakes in the AI native payment validation presentation',
            'Analyze this presentation and share the mistakes you find',
            'Review the contract for inconsistencies',
            'Summarize the nutrition guide',
            'Critique the pitch deck - what would you improve?',
            'Proofread the report and list every error',
            'Translate this document to Tamil',
            'List every action item mentioned in this document',
            'Compare this contract against our standard terms',
            "What's the overall tone of this document?",
            'Outline this presentation slide by slide',
            'Extract every date mentioned in this contract',
        ],
        input_modes=['text/plain'],
        output_modes=['application/json'],
    ),
]
