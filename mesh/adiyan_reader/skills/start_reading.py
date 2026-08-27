"""
start_reading's real body - registers a new reading_job and its first
nightly trigger. DataPart-only, not classify-able from free text - same
"no guessing a real key from prose" rule mesh/scheduler/skills/schedule_job.py's
own source_filename param already follows (see that file's own history):
source_filename has to already be a real, resolved <username>/<filename>
key (from Memory Agent's ingest_book), and phone_number has to be a real
number, not something an extraction LLM should ever be trusted to invent.

Recurrence follows mesh/scheduler/skills/run_routine.py's own established
pattern exactly: this agent re-registers itself with cron_trigger after
every fire (see read_next_page.py) rather than cron_trigger holding a
recurring schedule itself - cron_trigger's register_trigger is a one-shot
mechanism, the same way Scheduler Agent already treats it.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from croniter import croniter

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID, AGENT_URL, CRON_TRIGGER_URL
from mesh.adiyan_reader.tts import VOICES
from mesh.lib import config_sdk, permissions
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path


async def run(phone_number: str, source_filename: str, voice: Optional[str] = None) -> Dict[str, Any]:
    # voice=None means "no explicit choice" - stored as '' (not a real
    # voice name, matches _NO_VOICE_OVERRIDE in read_next_page.py) rather
    # than resolving default_voice once and freezing it into this job
    # forever. Confirmed live this session: the old behavior (resolve
    # default_voice here, store the resolved name) meant changing
    # default_voice in the dashboard later had zero effect on any
    # already-started job - the config lives in Mongo specifically so it
    # stays live, not so it gets copied into SQLite at creation time. A
    # caller who DID explicitly ask for a specific voice still gets that
    # exact one stored and respected forever, same as before - this only
    # changes the "didn't say" case.
    available_voices = await config_sdk.get_constant(
        AGENT_ID, 'available_voices', list(VOICES),
        description='The Orpheus voice names this deployment allows readers to pick from.',
    )
    stored_voice = voice if (voice is not None and voice in available_voices) else ''

    conn = db.connect(state_db_path(AGENT_ID))

    reading_hour = await config_sdk.get_constant(
        AGENT_ID, 'reading_hour', 0,
        description='Hour of day (0-23, local server time) the nightly page reading fires. 0 = midnight.',
    )
    cron_expression = f'0 {int(reading_hour)} * * *'
    next_run_at = croniter(cron_expression, datetime.now(timezone.utc)).get_next(datetime).isoformat()

    # Resume the existing job instead of creating a second one reading the
    # same book to the same number in parallel - confirmed live this
    # session that nothing else guards against this (a repeated "start
    # reading" request, or now the Orchestrator NL flow, would otherwise
    # spin up a duplicate every time). next_reading_at here is an
    # approximation (today's reading_hour applied fresh), not the resumed
    # job's actual already-registered trigger time - close enough to tell
    # the sender when to expect the next page, not something anything else
    # in this skill depends on being exact.
    existing = db.get_active_reading_job(conn, phone_number, source_filename)
    if existing is not None:
        return {
            'reading_job_id': existing['id'],
            'source_filename': source_filename,
            'voice': existing['voice'] or 'default',
            'current_page': existing['current_page'],
            'already_active': True,
            'first_reading_at': next_run_at,
        }

    job = db.create_reading_job(conn, phone_number, source_filename, stored_voice)

    token = permissions.mint_token(AGENT_ID, 'service')
    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that fires this agent\'s nightly reading and next-day quiz.',
    )
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': job['id'],
        'invoke_at': next_run_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'read_next_page',
        'params': {'reading_job_id': job['id']},
    }, token=token)

    return {
        'reading_job_id': job['id'],
        'source_filename': source_filename,
        'voice': stored_voice or 'default',
        'current_page': job['current_page'],
        'already_active': False,
        'first_reading_at': next_run_at,
    }
