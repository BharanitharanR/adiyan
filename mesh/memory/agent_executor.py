"""
Memory Agent's AgentExecutor. Same "every caller is a stranger, DataPart is
the fast path" design as mesh/scheduler/agent_executor.py - see that file's
module docstring. In practice, Memory Agent's real caller (Journal Agent)
already knows exactly what it wants and will always use the DataPart path;
free text is still supported for genuine A2A compliance, not because it's
the expected way in.
"""
from pathlib import Path
from typing import Any, Dict

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
    get_document_text,
    ingest,
    list_documents,
    recall,
    remember,
    resolve_document,
    search_document_chunks,
    search_kb,
    share_document,
)
from mesh.memory.skills_catalog import get_skills

AGENT_CODE_DIR = Path(__file__).parent


class RecallParams(BaseModel):
    contact_name: str = Field(description="The exact contact identifier to look up - not a display name guess.")
    query: str = Field(description="What to search for in their history, e.g. 'recent mood, work stress'.")
    top_k: int = Field(default=3, description="How many past snippets to retrieve.")


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

        result = handler(**params)
        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
