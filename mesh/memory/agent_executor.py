"""
Memory Agent's AgentExecutor. Same "every caller is a stranger, DataPart is
the fast path" design as mesh/scheduler/agent_executor.py - see that file's
module docstring. In practice, Memory Agent's real caller (Journal Agent)
already knows exactly what it wants and will always use the DataPart path;
free text is still supported for genuine A2A compliance, not because it's
the expected way in.
"""
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from a2a.helpers import (
    get_data_parts,
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from mesh.lib import config_sdk, permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.skill_router import route
from mesh.memory.constants import AGENT_ID
from mesh.memory.memory_index import KB_DEFAULT_TOP_K
from mesh.memory.skills import (
    get_book_page,
    get_document_text,
    ingest,
    ingest_book,
    list_documents,
    recall,
    remember,
    resolve_book,
    resolve_document,
    search_document_chunks,
    search_kb,
    share_document,
)
from mesh.memory.skills_catalog import get_skills

AGENT_CODE_DIR = Path(__file__).parent

# Skills whose access must be scoped to whoever's actually asking - see
# memory_index.py's own module docstring for the confirmed-live leak this
# closes. requester_id/is_owner are deliberately NOT caller-supplied params
# the way query/top_k are (a caller could just claim to be the owner or
# claim someone else's chat_id) - they're injected below from the token
# permissions.verify_token() already authenticated this call with,
# overridden only when the caller already supplied its own (real) values -
# see the setdefault() call below for exactly which caller that is and why.
_SCOPED_SKILLS = {
    'search_knowledge_base', 'share_knowledge_document', 'resolve_document',
    'get_document_text', 'search_document_chunks', 'list_documents',
}


class RecallParams(BaseModel):
    contact_name: str = Field(description="The exact contact identifier to look up - not a display name guess.")
    query: str = Field(description="What to search for in their history, e.g. 'recent mood, work stress'.")
    top_k: int = Field(default=3, description="How many past snippets to retrieve.")
    vertical_id: Optional[str] = Field(default=None, description="Never guess this from the text - only a DataPart caller that already knows it should ever set it.")


class SearchKBParams(BaseModel):
    query: str = Field(description="What to search for in the coach's uploaded knowledge base.")
    top_k: int = Field(default=KB_DEFAULT_TOP_K, description="How many chunks to retrieve.")


class ShareDocumentParams(BaseModel):
    query: str = Field(description="What document/topic to find and share, e.g. 'bicep curls' or 'nutrition guide'.")


EXTRACTION_SCHEMAS = {
    'recall_contact_memory': RecallParams,
    'search_knowledge_base': SearchKBParams,
    'share_knowledge_document': ShareDocumentParams,
}

# skill_id -> handler. ingest_document, resolve_document, get_document_text,
# search_document_chunks, and remember_interaction are all dispatchable here
# despite not being in SKILLS/EXTRACTION_SCHEMAS above - see
# mesh/memory/skills/ingest.py's own docstring for why a DataPart-only skill
# stays out of the classify pool.
SKILL_HANDLERS = {
    'recall_contact_memory': recall.run,
    'search_knowledge_base': search_kb.run,
    'share_knowledge_document': share_document.run,
    'ingest_document': ingest.run,
    'resolve_document': resolve_document.run,
    'get_document_text': get_document_text.run,
    'search_document_chunks': search_document_chunks.run,
    'remember_interaction': remember.run,
    'list_documents': list_documents.run,
    'ingest_book': ingest_book.run,
    'get_book_page': get_book_page.run,
    'resolve_book': resolve_book.run,
}


class MemoryAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.start_work()

        data_parts = get_data_parts(context.message.parts)
        if data_parts:
            payload = dict(data_parts[0])
            skill_id = payload.pop('skill_id', None)
            params: Dict[str, Any] = payload
        else:
            text = get_message_text(context.message)
            cfg = await config_sdk.load_stage_configs(AGENT_ID, load_runtime_config(AGENT_CODE_DIR))
            skills = await get_skills()
            try:
                skill_id, ambiguous, params = await route(
                    text, skills, EXTRACTION_SCHEMAS,
                    classify_cfg=cfg['classify_skill'],
                    extract_cfg=cfg['extract_parameters'],
                )
            except Exception as e:
                await updater.failed(new_text_message(f'Could not process request: {e}'))
                return
            if skill_id is None and ambiguous:
                options = ', '.join(ambiguous)
                await updater.requires_input(
                    new_text_message(f'Did you mean one of: {options}? Please clarify which one.')
                )
                return
            if skill_id is None:
                await updater.reject(new_text_message("None of my skills match that request."))
                return

        handler = SKILL_HANDLERS.get(skill_id)
        if handler is None:
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        claims = permissions.verify_token(context.metadata.get('token'))
        if not permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}'):
            await updater.reject(new_text_message('Not authorized for this.'))
            return

        if skill_id in _SCOPED_SKILLS:
            # setdefault, not an unconditional overwrite: a direct free-text
            # or DataPart call from Orchestrator has no requester_id/is_owner
            # of its own in params, so this token's own claims (Orchestrator
            # always mints with the real sender's chat_id/tier - see
            # rules_engine.check()) are the right answer. But a call
            # forwarded through Analysis Agent's ReAct loop (see
            # mesh/analysis/skills/analyze.py's _make_tools()) already
            # carries the REAL end-user's identity as an explicit param -
            # this call's own token claims would instead say sub='analysis',
            # tier='analysis_service' (Analysis Agent's own service
            # identity, not the human who actually asked), which must not
            # silently replace what Analysis Agent already resolved and
            # forwarded correctly.
            claims = claims or {}
            params.setdefault('requester_id', claims.get('sub'))
            params.setdefault('is_owner', claims.get('tier') == 'owner')

        # to_thread, not a plain call: every handler here is a normal sync
        # function, and ingest_book/ingest_document's Docling parse (OCR
        # especially) can run for minutes with no internal await point of
        # its own. Called directly, that fully occupies this process's one
        # event loop for its whole duration - confirmed live this session,
        # a multi-minute book ingestion left this agent unable to answer
        # even a plain search_knowledge_base call until it finished. A
        # thread doesn't make the ingestion itself faster, but PyTorch's
        # CPU work (what OCR actually spends its time in) releases the GIL
        # during computation, so a background thread genuinely lets this
        # loop keep serving other requests while it runs.
        result = await asyncio.to_thread(handler, **params)
        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
