"""
Example Agent - A2A server entrypoint. Same shape as every other agent's
server.py (copied from mesh/journal/server.py, the smallest real one).

Run from the repo root as `python -m mesh.example_agent.server`, then
either register it with mesh.agent_registry so the orchestrator can route
to it, or call it directly for testing:

    curl -s http://127.0.0.1:8440/.well-known/agent-card.json

Nothing in this file is example-agent-specific except the two `Example`
names and the description string - a new agent's server.py is this file
with its own AGENT_ID/name/skills swapped in.
"""
import asyncio
from pathlib import Path

from mesh.micro_habits.agent_executor import MicroHabitAgentExecutor
from mesh.micro_habits.constants import AGENT_ID, HOST, PORT
from mesh.micro_habits.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.micro_habits.agent_executor import MicroHabitAgentExecutor

AGENT_CODE_DIR = Path(__file__).parent


async def _load_startup_config() -> dict:
    # Every key in seed_config.json goes into Mongo right now, not lazily
    # the first time whatever branch happens to touch it - see
    # mesh/lib/config_sdk.py's seed_from_file(). This is also what makes
    # the descriptions in skills_catalog.py editable from the config
    # dashboard afterward, without touching this file again.
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
        'Micro Habit Agent - logs short micro-habit entries for the owner, e.g. "did 10 pushups", "no sugar today", "read for 15 min".',
        description='Micro Habit Agent - this logs short micro habit entries.',
    )
    await config_sdk.register_runnable(AGENT_ID, module=f'mesh.{AGENT_ID}.server', port=port)
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Micro Habit Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=MicroHabitAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
