"""
run_routine's real body - the piece that was raising NotImplementedError.

Resolves which job (by job_id if the caller already knows it - e.g.
cron_trigger's fire call - or by name_or_phrase via the same embedding
infrastructure schedule_job.py uses for dedup, here used for lookup
instead), decides whether this job's content should be delegated to Journal
Agent, composes the message, sends it via AdiyanAgent.notify_owner()
(mesh/lib/agent_sdk.py - the permission-checked path, not a direct
OpenWAService call), and re-registers the job's next occurrence with
cron_trigger - Scheduler Agent owns recurrence, not cron_trigger.

The Journal-or-generic decision reuses skill_router.classify() against
Journal Agent's own AgentSkill, exactly the mechanism a real registry would
generalize to later - just hardcoded to one candidate agent today, since the
registry idea is deliberately parked (see docs/AGENTS.md).
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from croniter import croniter
from langchain_ollama import ChatOllama
from pydantic import BaseModel

from mesh.journal.constants import AGENT_URL as JOURNAL_AGENT_URL
from mesh.journal.skills_catalog import get_skills as get_journal_skills
from mesh.lib import config_sdk, permissions
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.lib.skill_router import classify
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID, AGENT_URL, CRON_TRIGGER_URL
from mesh.scheduler.job_lookup import resolve_job
from mesh.scheduler.skills.schedule_job import AGENT_CODE_DIR, OLLAMA_URL, _seeded

# One instance, module-level - AGENT_ID never changes at runtime, and
# every method mints its own token internally against 'scheduler_service'
# (mesh/lib/permissions_config.json), never something this file does by
# hand anymore. See that tier's own description for why WhatsApp send is
# deliberately re-granted here, scoped to Scheduler alone, rather than
# left off entirely or reopened on the shared 'service' tier.
_agent = AdiyanAgent(AGENT_ID)


class GenericMessage(BaseModel):
    text: str


async def _wants_journal(description: str, cfg: Dict[str, Any]) -> bool:
    journal_skills = await get_journal_skills()
    choice = await classify(description, journal_skills, cfg)
    return choice.skill_id == 'craft_reflection_prompt'


def _looks_like_unfilled_template(text: str) -> bool:
    """True if text contains a literal bracket placeholder like '[Name]' -
    the tell that the model reached for mail-merge-style phrasing instead of
    writing something grounded in the job's actual description. Confirmed
    live: asking the model for a 'warm' message about a content-free
    description (e.g. "Log progress each day at 6pm") reliably produced
    exactly this pattern - "Hi [Name]!..." - never a real name, since
    nothing in the prompt ever supplies one."""
    return bool(re.search(r'\[[A-Za-z][A-Za-z ]{0,20}\]', text))


async def _compose_generic(description: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Honest fallback for anything Journal Agent doesn't cover - a plain
    reminder grounded only in the job's own description, nothing invented
    past that (see the "todos" job design discussion for why).

    Returns None instead of templated filler when the model can't produce
    real content - see _looks_like_unfilled_template's docstring. Silence
    here is deliberate, the same principle as never sending raw error text:
    a job with nothing concrete to report shouldn't manufacture fake warmth
    to fill the gap."""
    model = ChatOllama(
        model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'],
    ).with_structured_output(GenericMessage)
    seeded = _seeded('compose_generic_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'compose_generic_prompt_template', seeded['value'], description=seeded['description'],
    )
    try:
        prompt = template.format(description=description)
    except Exception:
        prompt = seeded['value'].format(description=description)
    result = await model.ainvoke(prompt)
    if _looks_like_unfilled_template(result.text):
        return None
    return result.text


async def _compose_message(job: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    if await _wants_journal(job['description'], cfg['select_composer']):
        try:
            result = await _agent.call_agent(JOURNAL_AGENT_URL, 'craft_reflection_prompt', {
                'contact_name': job['target'],
                'theme': None,
            })
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

    sent = False
    if message_text is not None:
        # agent.notify_owner() never raises - a failure here (owner's phone
        # unresolvable, WhatsApp not connected) now falls through to the
        # same silent 'skipped' outcome as message_text being None below,
        # rather than the RuntimeError this used to raise. That's a
        # deliberate change, not an oversight: never surfacing a raw
        # failure over WhatsApp already applies everywhere else in this
        # mesh (see mesh/lib/utilities/whatsapp/notify_owner.py's own
        # docstring) - a job that couldn't send this time re-registers
        # below and gets another chance next time regardless.
        sent = await _agent.notify_owner(message_text)
    # message_text is None: _compose_generic couldn't produce anything
    # grounded in the job's actual description (see
    # _looks_like_unfilled_template) - staying silent here rather than
    # sending manufactured "[Name]" filler, same principle as never sending
    # raw error text. Recurrence below still re-registers regardless -
    # this firing having nothing to say doesn't mean the next one won't.

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
        # run_routine's own signature reads job_id, not cron_trigger's
        # registration-level 'job_id' field above (that one's just the
        # trigger's own identity, used for lookup/cancellation - see
        # register_trigger's own docstring). cron_trigger used to inject
        # its 'job_id' into every fire's params for free; now that it
        # calls call_agent() like everything else in this mesh, params is
        # passed through exactly as given, so this needs to be explicit
        # here (same as AdiyanReader's start_reading already does with its
        # own 'reading_job_id').
        'params': {'job_id': job['id']},
    }, token=token)

    if message_text is None:
        status = 'skipped_no_content'
    elif sent:
        status = 'completed'
    else:
        # There was real content, but agent.notify_owner() returned False -
        # a different case from "nothing to say", worth distinguishing in
        # the result so this doesn't read as the message having been a
        # generic no-op when it was actually a send failure.
        status = 'send_failed'

    return {
        'job_id': job['id'],
        'routine_name': job['name'],
        'status': status,
        'result_summary': message_text,
    }
