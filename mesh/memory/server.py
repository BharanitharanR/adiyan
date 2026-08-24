"""
Memory Agent - A2A server entrypoint. Same shape as mesh/orchestrator/server.py -
card built in code via mesh.lib.card, task store via mesh.lib.bootstrap.

HOST/PORT/card description are Mongo-backed via config_sdk (mesh/memory/
constants.py's values are now only the fallback/first-seed defaults, same
pattern as every other agent already migrated) - fetched once here, before
anything binds a socket, not per-request (a listening port can't sensibly
change mid-process the way a prompt can).

Run from the repo root as `python -m mesh.memory.server`.
"""
import asyncio

from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.memory.agent_executor import MemoryAgentExecutor
from mesh.memory.constants import AGENT_ID, HOST, PORT
from mesh.memory.skills_catalog import get_skills
from mesh.observability.tracing import setup_tracing


async def _load_startup_config() -> dict:
    host = await config_sdk.get_constant(AGENT_ID, 'host', HOST)
    port = await config_sdk.get_constant(AGENT_ID, 'port', PORT)
    description = await config_sdk.get_constant(
        AGENT_ID, 'card_description',
        "Looks up what's known about one person or business from their past conversations.",
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Memory Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=MemoryAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        # Registry-side card now reflects a vertical change too, not just
        # this agent's own internal classify decisions - see
        # bootstrap.py's own module docstring.
        skills_refresher=get_skills,
    )
