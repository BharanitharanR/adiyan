"""
Orchestrator Agent - A2A server entrypoint. Same shape as every other
mesh/ agent's server.py.

Run from the repo root as `python -m mesh.orchestrator.server`. The
whatsapp MCP server (port 8425) should already be running - it pushes
incoming messages here, and handle_message replies through its
send_message tool.

HOST/PORT/card description/mcp_servers are Mongo-backed via config_sdk
(mesh/orchestrator/constants.py's values are now only the fallback/
first-seed defaults, same pattern as every other config this pilot agent
already migrated) - fetched once here, before anything binds a socket,
not per-request the way handle_message.py's own stage configs are (a
listening port can't sensibly change mid-process the way a prompt can).
"""
import asyncio
from pathlib import Path

from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.config import load_mcp_config
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing
from mesh.orchestrator import router
from mesh.orchestrator.agent_executor import OrchestratorAgentExecutor
from mesh.orchestrator.constants import AGENT_ID, HOST, PORT
from mesh.orchestrator.skills_catalog import get_skills

AGENT_CODE_DIR = Path(__file__).parent


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
        AGENT_ID, 'card_description', 'Routes an incoming message to the right agent and replies back.',
        description='What this agent does, shown in its A2A agent card.',
    )
    # Not consumed by any orchestrator code today (confirmed - grep turns
    # up nothing) - seeded anyway so it's on file for whatever eventually
    # reads it, same "migrate what's hardcoded, not just what's already
    # wired up" reasoning as everything else moved into config_sdk here.
    mcp_servers = await config_sdk.get_constant(
        AGENT_ID, 'mcp_servers', load_mcp_config(AGENT_CODE_DIR),
        description='Which MCP servers this agent connects to at startup - editing this needs a restart to take effect.',
    )
    skills = await get_skills()

    return {'host': host, 'port': port, 'description': description, 'mcp_servers': mcp_servers, 'skills': skills}


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    startup = asyncio.run(_load_startup_config())

    # Blocking - loads which agents/skills exist from the Agent Registry
    # before Orchestrator starts accepting real traffic. See router.py's
    # module docstring for why this is once-at-startup, not per-message.
    router.load_agent_pool()

    agent_card = adiyan_card(
        name='Orchestrator Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=OrchestratorAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        # Registry-side card now reflects a vertical change too, not just
        # this agent's own internal classify decisions - see
        # bootstrap.py's own module docstring.
        skills_refresher=get_skills,
    )
