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
from pathlib import Path

from mesh.adiyan_reader.agent_executor import AdiyanReaderAgentExecutor
from mesh.adiyan_reader.constants import AGENT_ID, HOST, PORT
from mesh.adiyan_reader.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing

logger = logging.getLogger('AdiyanReaderServer')

AGENT_CODE_DIR = Path(__file__).parent

# Startup catch-up of overdue reading jobs was REMOVED on 2026-09-15, on the
# owner's own instruction, after it was confirmed live to be a message-storm
# generator rather than a recovery mechanism.
#
# What it did: on every process start, find_overdue_reading_jobs() returned
# every active job whose next reading was in the past, and each one was fired
# immediately - then a background thread repeated that pass every 5 minutes.
# What that meant in practice: a night of agent restarts (orchestrator,
# analysis, memory and whatsapp_mcp were each restarted several times during
# one debugging session) re-fired all seven then-active reading jobs on every
# single boot, delivering bursts of book pages to four different real
# recipients who had not asked for them. The mechanism could not tell "this
# page was genuinely missed while the mesh was down" apart from "this process
# just restarted again," because both look identical to it: a job whose
# next_reading_at is in the past.
#
# Deliberately not replaced with a capped or time-windowed variant. A missed
# nightly page is a page the reader simply gets the following night - the cost
# of skipping one is a page's delay, while the cost of a wrong catch-up is
# unsolicited messages to other people's phones. cron_trigger's own scheduled
# fire remains the single path that sends a page; if it misses one, nothing
# retroactively replays it, which is now the intended behaviour rather than a
# gap to close.


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
