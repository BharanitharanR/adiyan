"""
Generic A2A client helper, shared by any agent under mesh/ that calls
another agent. Same reasoning as mcp_client.py: use the SDK's own client
(A2ACardResolver, create_client), not a hand-rolled JSON-RPC call.

Two entry points, for the two legitimate reasons to call another agent:
- call_agent(): a structured Part.data - the caller already knows exactly
  which skill and parameters it wants (e.g. Scheduler -> Journal, or
  cron_trigger's fire calls). No NLU needed on the receiving end.
- call_agent_with_text(): plain free text - the caller is forwarding a
  genuine stranger/human message (e.g. WhatsApp Connector -> whichever
  agent) and deliberately wants the receiving agent's OWN classify_skill/
  extract_parameters pipeline to interpret it, not to duplicate that logic
  in the caller.
"""
from typing import Any, Dict, Optional

import httpx
from google.protobuf import json_format

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_message, new_text_part
from a2a.types import Role, SendMessageRequest, Task, TaskState

# Confirmed live: the old 120s default was too short once Analysis Agent's
# analyse_this became a multi-step ReAct loop (mesh/analysis/skills/
# analyze.py) - up to MAX_STEPS sequential local-model calls (a decide call
# plus a compaction call per step) can genuinely take longer than two
# minutes on local hardware, and Orchestrator's own call to it used this
# same default. Explicitly generous for initial end-to-end validation, not
# a claim this is the right steady-state value - worth revisiting once
# real latency is actually observed rather than guessed at.
DEFAULT_TIMEOUT_SECONDS = 3 * 60 * 60  # 3 hours


async def _send_and_await(agent_url: str, message, timeout: float, token: Optional[str] = None) -> Task:
    async with httpx.AsyncClient(timeout=timeout) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=agent_url)
        card = await resolver.get_agent_card()

        client = await create_client(
            agent=card,
            client_config=ClientConfig(streaming=False, httpx_client=httpx_client),
        )
        request = SendMessageRequest(message=message)
        if token:
            # SendMessageRequest.metadata is a real protobuf Struct field,
            # not repurposed - confirmed it survives to the receiving
            # agent's RequestContext.metadata dict before this was relied
            # on. This is how a caller's permission token travels alongside
            # a call without touching the message's own Part payload.
            json_format.ParseDict({'token': token}, request.metadata)

        task: Task = None
        async for chunk in client.send_message(request):
            # Each chunk is a StreamResponse - a oneof wrapper (task/message/
            # status_update/artifact_update), not a Task directly. Confirmed
            # live: treating chunk itself as the Task raised
            # AttributeError('status') on every call through this function.
            if chunk.WhichOneof('payload') == 'task':
                task = chunk.task
        await client.close()

        if task is None:
            raise RuntimeError(f'No task response from {agent_url}')
        return task


def _extract_result(task: Task, agent_url: str, what: str) -> Dict[str, Any]:
    if task.status.state != TaskState.TASK_STATE_COMPLETED:
        error_text = get_message_text(task.status.message) or 'unknown error'
        raise RuntimeError(f"{agent_url}'s {what} failed: {error_text}")
    if not task.artifacts:
        return {}
    data_parts = get_data_parts(task.artifacts[0].parts)
    return data_parts[0] if data_parts else {}


async def call_agent(
    agent_url: str,
    skill_id: str,
    params: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Calls another agent's skill directly via a structured Part.data.
    Raises RuntimeError on a failed/rejected/empty task; returns the
    artifact's data dict on success. token: a permission token
    (mesh/lib/permissions.py) - the receiving agent rejects the call
    without one, or with one that doesn't cover this skill."""
    message = new_message(
        parts=[new_data_part({'skill_id': skill_id, **params})],
        role=Role.ROLE_USER,
    )
    task = await _send_and_await(agent_url, message, timeout, token)
    return _extract_result(task, agent_url, skill_id)


async def call_agent_with_text(
    agent_url: str,
    text: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Forwards free text to another agent, letting its own classify/extract
    pipeline interpret it. Same result shape and failure handling as
    call_agent(). token: see call_agent() - the receiving agent doesn't
    know which skill this resolves to until it classifies the text itself,
    so the permission check happens after that, not before."""
    message = new_message(parts=[new_text_part(text)], role=Role.ROLE_USER)
    task = await _send_and_await(agent_url, message, timeout, token)
    return _extract_result(task, agent_url, 'text message')
