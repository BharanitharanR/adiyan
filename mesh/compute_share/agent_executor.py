"""
compute_share's AgentExecutor. Same DataPart-fast-path/permission-check
shape as every other agent under mesh/ (copied from mesh/adiyan_reader/
agent_executor.py) - every real call here is agent-to-agent (another
Adiyan instance calling announce_peer/offload/run_inference/gossip), so
free text has nothing to classify against, same reasoning as
AdiyanReader's own executor.

PUBLIC_SKILLS is the one real deviation from every other agent in this
mesh, and it's deliberate, not an oversight: run_inference and
announce_peer are the two calls a genuinely different person's Adiyan
instance needs to make, and the internal permission system (a token
signed with THIS install's own PERMISSIONS_JWT_SECRET) cannot
authenticate a caller signed with a *different* install's secret at
all - confirmed live, this isn't a gap that can be closed by adding the
right tier, it's structural. Matching the deliberate design choice
documented in README.md ("no peer authentication, BitTorrent doesn't
authenticate peers either - it verifies content, not identity"), these
two skills accept any caller, authenticated or not. The safety boundary
is what's exposed, not who's calling: run_inference's whole signature is
(prompt in, text out) with no document/memory/conversation access
reachable through it, and announce_peer only ever writes to this
instance's own local peer table. offload and gossip stay behind the
normal token check - offload triggers an outbound spend of someone
else's compute on THIS instance's behalf, and gossip is a self-
recurring internal job (cron_trigger's own service token) - neither is
meant to be triggered by an arbitrary stranger.
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
from mesh.compute_share.skills import announce_peer, gossip, offload, run_inference
from mesh.lib import permissions
from mesh.lib.errors import describe_exception

SKILL_HANDLERS = {
    'run_inference': run_inference.run,
    'announce_peer': announce_peer.run,
    'offload': offload.run,
    'gossip': gossip.run,
}

PUBLIC_SKILLS = {'run_inference', 'announce_peer'}


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

        if skill_id not in PUBLIC_SKILLS:
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
