"""
run_routine's real body - the piece that was raising NotImplementedError.

Resolves which job (by job_id if the caller already knows it - e.g.
cron_trigger's fire call - or by name_or_phrase via the same embedding
infrastructure schedule_job.py uses for dedup, here used for lookup
instead), decides whether this job's content should be delegated to Journal
Agent, composes the message, sends it via
mesh/lib/utilities/whatsapp/openwa_service.py, and re-registers the job's
next occurrence with cron_trigger - Scheduler Agent owns recurrence, not
cron_trigger.

The Journal-or-generic decision reuses skill_router.classify() against
Journal Agent's own AgentSkill, exactly the mechanism a real registry would
generalize to later - just hardcoded to one candidate agent today, since the
registry idea is deliberately parked (see mesh/AGENTS.md).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from croniter import croniter
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.journal.constants import AGENT_URL as JOURNAL_AGENT_URL
from mesh.journal.skills_catalog import get_skills as get_journal_skills
from mesh.lib import config_sdk, permissions
from mesh.lib.a2a_client import call_agent
from mesh.lib.config import load_runtime_config
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.lib.skill_router import classify
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID, AGENT_URL, CRON_TRIGGER_URL
from mesh.scheduler.job_lookup import resolve_job
from mesh.scheduler.skills.schedule_job import AGENT_CODE_DIR, OLLAMA_URL

OPENWA_URL = 'http://localhost:2785'
OPENWA_SESSION_NAME = 'adiyan'


class GenericMessage(BaseModel):
    text: str


async def _wants_journal(description: str, cfg: Dict[str, Any]) -> bool:
    journal_skills = await get_journal_skills()
    choice = await classify(description, journal_skills, cfg)
    return choice.skill_id == 'craft_reflection_prompt'


async def _compose_generic(description: str, cfg: Dict[str, Any]) -> str:
    """Honest fallback for anything Journal Agent doesn't cover - a plain
    reminder grounded only in the job's own description, nothing invented
    past that (see the "todos" job design discussion for why)."""
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(GenericMessage)
    result = await model.ainvoke(
        f'Write a short, warm WhatsApp reminder message for this: "{description}". '
        'Do not invent specific details (like actual to-do items) that are not '
        'in the description itself - this is a nudge, not a report.'
    )
    return result.text


async def _compose_message(job: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    if await _wants_journal(job['description'], cfg['select_composer']):
        try:
            # Service token - run_routine fires on a schedule (no WhatsApp
            # caller in the loop at all here) or was already authorized via
            # cron_trigger's own service-token call into this skill.
            token = permissions.mint_token('scheduler', 'service')
            result = await call_agent(JOURNAL_AGENT_URL, 'craft_reflection_prompt', {
                'contact_name': job['target'],
                'theme': None,
            }, token=token)
            question = result.get('question')
            if question:
                return question
        except RuntimeError:
            pass  # Journal Agent unreachable/erroring - fall through to generic
    return await _compose_generic(job['description'], cfg['compose_message'])


async def run(job_id: Optional[str] = None, name_or_phrase: Optional[str] = None) -> Dict[str, Any]:
    if not job_id and not name_or_phrase:
        raise ValueError('run_routine needs either job_id or name_or_phrase')

    conn = db.connect(state_db_path(AGENT_ID))
    job = await resolve_job(conn, job_id, name_or_phrase)

    if job['target'] != 'self':
        # Every job schedule_job creates today has target='self' - this is
        # defensive, not reachable yet, same honesty as
        # schedule_job.TargetNotResolvableError rather than silently guessing
        # who else to send to.
        raise NotImplementedError(f"Cannot resolve target '{job['target']}' - only 'self' is currently supported.")

    cfg = await config_sdk.load_stage_configs(AGENT_ID, load_runtime_config(AGENT_CODE_DIR))
    message_text = await _compose_message(job, cfg)

    openwa = OpenWAService(base_url=OPENWA_URL, api_key='', session_name=OPENWA_SESSION_NAME)
    chat_id = await openwa.get_own_chat_id()
    if chat_id is None:
        raise RuntimeError('Could not resolve the owner\'s own WhatsApp chat - is WhatsApp connected?')
    await openwa.send_message(chat_id, message_text)

    # Recurrence - Scheduler Agent's own responsibility, not cron_trigger's.
    # A one-time job (never actually built as such today, but defensive)
    # would skip this; every job schedule_job creates is recurring.
    next_run_at = croniter(job['resolved_schedule'], datetime.now(timezone.utc)).get_next(datetime).isoformat()
    db.update_next_run(conn, job['id'], next_run_at)
    token = permissions.mint_token('scheduler', 'service')
    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that actually fires scheduled jobs at their due time.',
    )
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': job['id'],
        'invoke_at': next_run_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'run_routine',
        'params': {},
    }, token=token)

    return {
        'job_id': job['id'],
        'routine_name': job['name'],
        'status': 'completed',
        'result_summary': message_text,
    }
