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
from pathlib import Path

from mesh.adiyan_reader.agent_executor import AdiyanReaderAgentExecutor
from mesh.adiyan_reader.constants import AGENT_ID, HOST, PORT
from mesh.adiyan_reader.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing

AGENT_CODE_DIR = Path(__file__).parent


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
