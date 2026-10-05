"""
Genie - A2A server entrypoint. Same shape as every other agent's
server.py (copied from mesh/journal/server.py, the smallest real one).

Run from the repo root as `python -m mesh.genie.server`, then
either register it with mesh.agent_registry so the orchestrator can route
to it, or call it directly for testing:

    curl -s http://127.0.0.1:8442/.well-known/agent-card.json

Standard agent server.py - AGENT_ID/name/description come from
constants.py and the config store; the rest is shared wiring.
"""
import asyncio
from pathlib import Path

from mesh.genie.agent_executor import GenieExecutor
from mesh.genie.constants import AGENT_ID, HOST, PORT
from mesh.genie.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path

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
    # Belt-and-suspenders registration. mesh/tools/runnable_agents.py (which
    # mesh/start_all.sh runs) already discovers this agent by statically
    # scanning mesh/*/constants.py for AGENT_ID + PORT, so start_all.sh
    # picks it up without this agent ever having been run. This call
    # additionally covers the case that static scan can't: an agent whose
    # PORT is a non-literal (e.g. int(os.environ.get(...))), which the scan
    # skips. No-op beyond a Mongo write; safe if the config store is down.
    await config_sdk.register_runnable(AGENT_ID, module=f'mesh.{AGENT_ID}.server', port=port)
    description = await config_sdk.get_constant(
        AGENT_ID, 'card_description',
        'Gives a child short Socratic hints for Daily Practice app questions, never the answer. Called only by the Daily Practice app.',
        description='What this agent does, shown in its A2A agent card.',
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Genie',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=GenieExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
