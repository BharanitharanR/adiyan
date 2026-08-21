"""
cron_trigger - an MCP server, not an A2A agent. It has no AgentCard, no
skills, no reasoning: it holds a durable list of (job_id, invoke_at,
target_agent_url, skill_id, params) and, at the registered datetime, calls
the target agent directly. Scheduler Agent (or any future agent with
scheduled behavior) is its MCP client via register_trigger.

Must run as a persistent process, not spawned fresh per call like most MCP
servers in this repo (core/mcp_tools.py's duckduckgo/crawl4ai pool) - the
whole point is remembering to act *after* register_trigger returns, possibly
days later. Same reasoning that already forced workspace-mcp (Gmail/Calendar)
into a persistent streamable-http server instead of stdio - see
services/workspace_mcp_service.py's own docstring for the identical argument.

register_trigger is deliberately single-shot: invoke_at is one specific
datetime, not a recurring cron expression. cron_trigger never parses
schedules - Scheduler Agent owns all recurrence, and re-registers the next
occurrence itself after each successful fire. This keeps cron_trigger
genuinely dumb: wake once, at this exact time, call this URL.

Firing uses plain httpx, not the a2a-sdk client (mesh/lib doesn't have an
A2A-client helper yet) - a deliberate deviation from "use the SDK for both
sides," acceptable here because the call is already fully precise (a
DataPart, not free text needing classification) and this component is meant
to stay minimal. Worth revisiting if an a2a-sdk-based client helper gets
built for mesh/lib later.

Run from the repo root as `python -m mesh.mcp.cron_trigger.server`.
Requires: pip install "mcp[cli]" apscheduler sqlalchemy httpx
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict

import httpx
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from mcp.server.fastmcp import Context, FastMCP

from mesh.lib import permissions
from mesh.lib.paths import mcp_state_db_path

SERVER_NAME = 'cron_trigger'
HOST = '127.0.0.1'
PORT = 8421

# Late firing (process was down, or busy past the exact minute) is tolerated
# up to this many seconds - the "+/- 2 minutes" tolerance settled on. This is
# a lateness allowance, not a symmetric window: waking every 60s already
# means a job can't be mistaken for due more than ~1 minute early.
MISFIRE_GRACE_SECONDS = 120

logger = logging.getLogger(SERVER_NAME)

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)

_scheduler = AsyncIOScheduler(
    jobstores={'default': SQLAlchemyJobStore(url=f'sqlite:///{mcp_state_db_path(SERVER_NAME)}')}
)


async def _fire(job_id: str, target_agent_url: str, skill_id: str, params: Dict[str, Any]) -> None:
    """Called by APScheduler when a registered job is due. A plain A2A
    message/send call carrying a structured Part.data - no NLU needed, this
    call was already precise when it was registered."""
    # A scheduled fire has no WhatsApp identity behind it at all - a
    # service token, same as every other purely-internal call in this
    # mesh. 'metadata' sits alongside 'message' in SendMessageRequest
    # (confirmed against a2a_pb2's own field list) - the raw-JSON-RPC
    # equivalent of what a2a_client.py does with the SDK's request object.
    token = permissions.mint_token('service', 'service')
    payload = {
        'jsonrpc': '2.0',
        'id': job_id,
        'method': 'message/send',
        'params': {
            'message': {
                'role': 'ROLE_USER',
                'parts': [{'data': {'skill_id': skill_id, 'job_id': job_id, **params}}],
            },
            'metadata': {'token': token},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(target_agent_url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as e:
        # Firing failure is logged, not retried here - Scheduler Agent owns
        # what "job failed to fire" means for its own domain (job_data,
        # whether to re-register). cron_trigger's job for this occurrence
        # ends here either way; it does not itself decide to retry.
        logger.error(f'Failed to fire job {job_id} at {target_agent_url}: {e}')


@mcp.tool()
def register_trigger(
    job_id: str,
    invoke_at: str,
    target_agent_url: str,
    skill_id: str,
    ctx: Context,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Wakes once, at invoke_at (ISO 8601 datetime), and invokes
    target_agent_url's skill_id with job_id via a direct A2A call. Calling
    this again with the same job_id replaces the prior registration - the
    caller is responsible for re-registering the next occurrence after each
    fire; this server does not know about recurrence."""
    permissions.enforce_mcp_permission(ctx, 'mcp.cron_trigger.register_trigger')
    run_date = datetime.fromisoformat(invoke_at)
    _scheduler.add_job(
        _fire,
        trigger='date',
        run_date=run_date,
        args=[job_id, target_agent_url, skill_id, params or {}],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    return {'registered': True, 'job_id': job_id, 'invoke_at': invoke_at}


@mcp.tool()
def remove_trigger(job_id: str, ctx: Context) -> Dict[str, Any]:
    """Cancels a previously registered trigger for job_id, if one exists.
    Not finding one isn't an error - the job may have already fired (a
    one-shot registration, consumed the moment it ran) or never been
    registered at all; either way the caller's intent (this job_id should
    not fire) is already satisfied."""
    permissions.enforce_mcp_permission(ctx, 'mcp.cron_trigger.remove_trigger')
    try:
        _scheduler.remove_job(job_id)
        return {'removed': True, 'job_id': job_id}
    except JobLookupError:
        return {'removed': False, 'job_id': job_id, 'reason': 'no such registration'}


async def main() -> None:
    # Started inside the same running loop mcp's async server uses, not
    # before it - AsyncIOScheduler binds to whichever loop is running when
    # .start() is called.
    _scheduler.start()
    await mcp.run_streamable_http_async()


if __name__ == '__main__':
    asyncio.run(main())
