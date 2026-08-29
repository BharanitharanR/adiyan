"""
Config Agent - A2A server entrypoint. Same shape as mesh/journal/server.py.

Run from the repo root as `python -m mesh.config_agent.server`. MongoDB
should already be running - query_config/update_config both go through
mesh/lib/config_sdk.py, which degrades gracefully (not fails) if it isn't.
"""
import asyncio
from pathlib import Path

from mesh.config_agent.agent_executor import ConfigAgentExecutor
from mesh.config_agent.constants import AGENT_ID, HOST, PORT
from mesh.config_agent.skills_catalog import SKILLS
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing

AGENT_CODE_DIR = Path(__file__).parent


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    # Every key in seed_config.json goes into Mongo right now, not lazily
    # the first time onboard_mcp_server happens to run - see
    # mesh/lib/config_sdk.py's seed_from_file().
    asyncio.run(config_sdk.seed_from_file(AGENT_ID, AGENT_CODE_DIR))

    agent_card = adiyan_card(
        name='Config Agent',
        description='Owner-only: query or update a live agent setting, prompt, or toggle.',
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=ConfigAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
