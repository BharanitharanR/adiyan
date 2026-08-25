"""
delete_job's real body. Removes the job from state.db AND cancels its
registration in cron_trigger - deleting only the first would leave a stale
trigger that still fires later and calls run_routine for a job_id that no
longer exists (this was the exact manual two-database cleanup done by hand
earlier in this build, before this skill existed).
"""
from typing import Any, Dict, Optional

from mesh.lib import config_sdk, permissions
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID, CRON_TRIGGER_URL
from mesh.scheduler.job_lookup import resolve_job


async def run(job_id: Optional[str] = None, name_or_phrase: Optional[str] = None) -> Dict[str, Any]:
    if not job_id and not name_or_phrase:
        raise ValueError('delete_job needs either job_id or name_or_phrase')

    conn = db.connect(state_db_path(AGENT_ID))
    job = await resolve_job(conn, job_id, name_or_phrase)

    db.delete_job(conn, job['id'])
    # A service token - the caller's own permission to delete_job was
    # already checked at the agent_executor boundary; this internal call
    # to cron_trigger is Scheduler acting on that already-authorized
    # request, not a second thing the original caller needs rights to.
    token = permissions.mint_token('scheduler', 'service')
    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that actually fires scheduled jobs at their due time.',
    )
    await call_tool(cron_trigger_url, 'remove_trigger', {'job_id': job['id']}, token=token)

    return {
        'job_id': job['id'],
        'routine_name': job['name'],
        'deleted': True,
    }
