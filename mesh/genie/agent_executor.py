"""
Genie's AgentExecutor - hints for the Daily Practice tablet app.

Two deliberate differences from the example agent this was scaffolded from:

1. Structured calls only (a DataPart with skill_id + params), no
   plain-language routing. The only caller is the Daily Practice app, which
   always knows exactly what it wants; a WhatsApp message routed here by
   Orchestrator has nothing to classify against and is refused.

2. A device key instead of a permission token, the same structural reason
   compute_share's PUBLIC_SKILLS exist: the tablet is not a WhatsApp
   identity, so it can never hold a token minted per message by
   Orchestrator's rules_engine. Instead the app sends the key a grown-up
   copied from this machine's vault (GENIE_DEVICE_KEY) in the request's
   metadata, compared in constant time. What's exposed is equally narrow:
   question text (or a recording) in, one checked hint (or a check of
   the recording) out - no memory, documents, conversations or other
   agents reachable through it.
"""
import hmac
from typing import Any, Dict

from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from mesh.genie.skills import family, listen_and_check, parent, socratic_nudge
from mesh.lib.secrets_vault import get_secret

DEVICE_KEY_SECRET = 'GENIE_DEVICE_KEY'
SKILL_HANDLERS = {
    'socratic_nudge': socratic_nudge.run,
    'listen_and_check': listen_and_check.run,
    'parent_verify_start': parent.verify_start,
    'parent_verify_confirm': parent.verify_confirm,
    'parent_status': parent.status,
    'parent_remove': parent.remove,
    'notify_parent': parent.notify,
    'ping': family.ping,
    'peers': family.peers,
    'announce': family.announce,
}
# Real work counts towards this genie's load, which ping reports to a racing caller.
WORK_SKILLS = {'socratic_nudge', 'listen_and_check'}


def device_key_ok(sent: Any) -> bool:
    expected = get_secret(DEVICE_KEY_SECRET)
    if not expected or not isinstance(sent, str) or not sent:
        return False
    return hmac.compare_digest(sent.encode(), expected.encode())


class GenieExecutor(AgentExecutor):
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
                'Genie only answers the Daily Practice app (a structured call with skill_id), not chat messages.'
            ))
            return
        payload = dict(data_parts[0])
        skill_id = payload.pop('skill_id', None)
        params: Dict[str, Any] = payload

        handler = SKILL_HANDLERS.get(skill_id)
        if handler is None:
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        if not device_key_ok((context.metadata or {}).get('device_key')):
            await updater.reject(new_text_message('Not authorized: wrong or missing device key.'))
            return

        try:
            if skill_id in WORK_SKILLS:
                async with family.busy():
                    result = await handler(**params)
            else:
                result = await handler(**params)
        except TypeError as e:
            await updater.failed(new_text_message(f'Bad request for {skill_id}: {e}'))
            return
        except Exception as e:
            await updater.failed(new_text_message(f'{skill_id} failed: {e}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not designed for this agent.')
