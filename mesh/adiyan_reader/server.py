"""
AdiyanReader - A2A server entrypoint. Same shape as mesh/journal/server.py.

Reads books uploaded to Memory Agent (via its ingest_book skill) back page
by page, as real WhatsApp voice notes synthesized locally with Orpheus TTS
(mesh/adiyan_reader/tts.py), then follows up the next morning with
comprehension questions preloaded when that page was read.

Run from the repo root as `python -m mesh.adiyan_reader.server`. Memory
Agent (port 8423), the WhatsApp MCP, and cron_trigger MCP should already be
running.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

from mesh.adiyan_reader import db
from mesh.adiyan_reader.agent_executor import AdiyanReaderAgentExecutor
from mesh.adiyan_reader.constants import AGENT_ID, HOST, PORT
from mesh.adiyan_reader.skills import read_next_page
from mesh.adiyan_reader.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import state_db_path, tasks_db_path
from mesh.observability.tracing import setup_tracing

logger = logging.getLogger('AdiyanReaderServer')

AGENT_CODE_DIR = Path(__file__).parent

# Same reasoning as mesh/scheduler/server.py's own CATCH_UP_RETRY_SECONDS: a
# startup-only catch-up attempt that fails once (e.g. WhatsApp's session
# hadn't reconnected yet at that exact moment) leaves the job stuck
# permanently otherwise - find_overdue_reading_jobs() only runs again on the
# next full process restart without this retry loop.
CATCH_UP_RETRY_SECONDS = 5 * 60


async def _catch_up_overdue_reading_jobs() -> None:
    """See db.find_overdue_reading_jobs()'s own docstring - catches a
    nightly page that mcp/cron_trigger's own misfire handling silently
    dropped while this mesh was down. Best-effort per job: one failing job
    must not block the others or stop this server from starting."""
    conn = db.connect(state_db_path(AGENT_ID))
    overdue = db.find_overdue_reading_jobs(conn)
    for job in overdue:
        try:
            await read_next_page.run(reading_job_id=job['id'])
            logger.info(f"Caught up overdue reading job {job['id']} ({job['source_filename']!r})")
        except Exception as e:
            logger.warning(f"Could not catch up overdue reading job {job['id']}, will retry: {e}")


def _catch_up_retry_loop() -> None:
    while True:
        time.sleep(CATCH_UP_RETRY_SECONDS)
        try:
            asyncio.run(_catch_up_overdue_reading_jobs())
        except Exception as e:
            logger.warning(f'Catch-up retry pass failed: {e}')


async def _load_startup_config() -> dict:
    # Every key in seed_config.json goes into Mongo right now, not lazily
    # the first time whatever branch happens to touch it - see
    # mesh/lib/config_sdk.py's seed_from_file().
    await config_sdk.seed_from_file(AGENT_ID, AGENT_CODE_DIR)
    host = await config_sdk.get_constant(
        AGENT_ID, 'host', HOST,
        description='Which network interface this agent binds to. Changing this needs a restart to take effect.',
    )
    port = await config_sdk.get_constant(
        AGENT_ID, 'port', PORT,
        description='Which port this agent listens on. Changing this needs a restart to take effect.',
    )
    description = await config_sdk.get_constant(
        AGENT_ID, 'card_description',
        'Reads an uploaded book back page by page as nightly WhatsApp voice notes, with next-day comprehension questions.',
        description='What this agent does, shown in its A2A agent card.',
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    asyncio.run(_catch_up_overdue_reading_jobs())
    threading.Thread(target=_catch_up_retry_loop, daemon=True).start()

    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='AdiyanReader',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=AdiyanReaderAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
