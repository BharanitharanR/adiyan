"""
Orchestrator Agent's AgentExecutor. Same design as every other agent under
mesh/ - see mesh/scheduler/agent_executor.py's module docstring for the
DataPart-fast-path / stranger-caller reasoning. In practice, Orchestrator's
real caller (whatsapp MCP's webhook push) always uses the DataPart path -
it already knows the exact text and chat_id, no NLU needed.
"""
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

from mesh.lib.config import load_runtime_config
from mesh.lib.errors import describe_exception
from mesh.lib.skill_router import route
from mesh.orchestrator.skills import handle_message
from mesh.orchestrator.skills_catalog import get_skills

AGENT_CODE_DIR = Path(__file__).parent


class HandleMessageParams(BaseModel):
    text: str = Field(description="The incoming message text to route and respond to.")
    chat_id: str = Field(description="Where to send the reply - the chat this message came from.")
    image: Optional[Dict[str, Any]] = Field(
        default=None,
        description="An attached image, if any, as {mimetype, data (base64)}. "
        "Almost never present via free text - realistically only set on the "
        "DataPart fast path from WhatsApp MCP's webhook.",
    )


EXTRACTION_SCHEMAS = {'handle_message': HandleMessageParams}


class OrchestratorAgentExecutor(AgentExecutor):
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
            cfg = load_runtime_config(AGENT_CODE_DIR)
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

        if skill_id != 'handle_message':
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        try:
            result = await handle_message.run(**params)
        except Exception as e:
            await updater.failed(new_text_message(f'handle_message failed: {describe_exception(e)}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
