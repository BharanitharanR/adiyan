"""AdiyanReader's AgentSkill catalog.

Deliberately empty today - every real skill here (start_reading,
read_next_page, dispatch_questions) needs an already-resolved real value
(a genuine source_filename from Memory Agent's ingest_book, a real phone
number, a reading_job_id/page_number cron_trigger already knows) that a
free-text extraction LLM has no safe way to produce on its own - the same
"never let the model guess a real key from prose" rule already established
for mesh/scheduler/skills/schedule_job.py's source_filename param and every
DataPart-only skill in mesh/memory/skills_catalog.py's own docstring.

A future WhatsApp-driven "read me this book every night" flow belongs in
Orchestrator (resolving the book name via Memory Agent's resolve_document,
same shape as handle_message.py's existing kb_pending upload-and-instruct
combination), not in this agent's own classify step - not yet built.

Structured identically to every other agent's skills_catalog.py (async
get_skills(), config_sdk-backed) so adding a real classify-able skill later
is a drop-in, not a redesign."""
from typing import List

from a2a.types import AgentSkill


async def get_skills() -> List[AgentSkill]:
    return []
