"""
Journal Agent - A2A server entrypoint. Same shape as mesh/scheduler/server.py.

Run from the repo root as `python -m mesh.journal.server`. Memory Agent
(port 8423) should already be running - craft_reflection_prompt calls it
directly.
"""
import asyncio

from mesh.journal.agent_executor import JournalAgentExecutor
from mesh.journal.constants import AGENT_ID, HOST, PORT
from mesh.journal.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing


async def _load_startup_config() -> dict:
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
        "Crafts a tailored journaling question for one person, personalized from what's known about them if anything is.",
        description='What this agent does, shown in its A2A agent card.',
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Journal Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=JournalAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
