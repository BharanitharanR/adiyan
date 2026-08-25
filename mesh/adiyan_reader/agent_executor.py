"""
AdiyanReader's AgentExecutor. Same "every caller is a stranger, DataPart is
the fast path" design as mesh/scheduler/agent_executor.py - see that file's
module docstring. In practice every real call into this agent arrives via
DataPart (cron_trigger's own fire calls, or a future Orchestrator flow that
already resolved a real source_filename/phone_number) - see
skills_catalog.py's own docstring for why free text has nothing to
classify against here today.
"""
from typing import Any, Dict

from a2a.helpers import (
    get_data_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.skills import dispatch_questions, read_next_page, start_reading
from mesh.adiyan_reader.skills_catalog import get_skills
from mesh.lib import permissions

SKILL_HANDLERS = {
    'start_reading': start_reading.run,
    'read_next_page': read_next_page.run,
    'dispatch_questions': dispatch_questions.run,
}


class AdiyanReaderAgentExecutor(AgentExecutor):
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
            # skills_catalog.get_skills() is deliberately empty today - no
            # real skill here is safe to guess parameters for from prose -
            # so free text has nothing to classify against, ever. Rejected
            # immediately rather than spending an LLM call on a classify
            # prompt with zero candidate skills, which would always come
            # back "no match" anyway.
            skills = await get_skills()
            if not skills:
                await updater.reject(new_text_message(
                    'AdiyanReader has no skills reachable from free text yet - '
                    'this needs an already-resolved book/phone number, not a description.'
                ))
                return
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

        try:
            result = await handler(**params)
        except Exception as e:
            await updater.failed(new_text_message(f'{skill_id} failed: {e}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
