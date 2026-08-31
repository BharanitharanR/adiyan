"""
compute_share's AgentExecutor. Same DataPart-fast-path/permission-check
shape as every other agent under mesh/ (copied from mesh/adiyan_reader/
agent_executor.py) - every real call here is agent-to-agent (another
Adiyan instance calling announce_peer/offload/run_inference), so free
text has nothing to classify against, same reasoning as AdiyanReader's
own executor.
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

from mesh.compute_share.constants import AGENT_ID
from mesh.compute_share.skills import announce_peer, offload, run_inference
from mesh.lib import permissions
from mesh.lib.errors import describe_exception

SKILL_HANDLERS = {
    'run_inference': run_inference.run,
    'announce_peer': announce_peer.run,
    'offload': offload.run,
}


class ComputeShareAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.start_work()

        data_parts = get_data_parts(context.message.parts)
        if not data_parts:
            await updater.reject(new_text_message(
                'compute_share only accepts pre-resolved calls (skill_id + params), not free text.'
            ))
            return
        payload = dict(data_parts[0])
        skill_id = payload.pop('skill_id', None)
        params: Dict[str, Any] = payload

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
            await updater.failed(new_text_message(f'{skill_id} failed: {describe_exception(e)}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
