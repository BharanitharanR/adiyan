"""
Analysis Agent - A2A server entrypoint. Same shape as mesh/journal/server.py.

Run from the repo root as `python -m mesh.analysis.server`. Memory Agent
(port 8423) should already be running - analyse_this calls it directly
(resolve_document, get_document_text, list_documents, recall_contact_memory).

HOST/PORT/card description/mcp_servers are Mongo-backed via config_sdk
(mesh/analysis/constants.py's values are now only the fallback/first-seed
defaults, same pattern as mesh/orchestrator/server.py) - fetched once
here, before anything binds a socket.
"""
import asyncio
from pathlib import Path

from mesh.analysis.agent_executor import AnalysisAgentExecutor
from mesh.analysis.constants import AGENT_ID, HOST, PORT
from mesh.analysis.skills_catalog import get_skills
from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.config import load_mcp_config
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing

AGENT_CODE_DIR = Path(__file__).parent


async def _load_startup_config() -> dict:
    # Every key in seed_config.json goes into Mongo right now, not lazily
    # the first time whatever ReAct branch happens to touch it - a
    # constant should show up on the config dashboard the moment this
    # agent boots, not only after some rare code path has run once. See
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
        "General-purpose reasoning over documents, conversation memory, and the wider agent mesh - "
        "Orchestrator's fallback for anything nothing more specific handles.",
        description='What this agent does, shown in its A2A agent card.',
    )
    # Not consumed by any analysis code today (mcp_config.json is currently
    # {"mcp_servers": []}) - seeded anyway, same "migrate what's hardcoded,
    # not just what's already wired up" reasoning as Orchestrator's own.
    mcp_servers = await config_sdk.get_constant(
        AGENT_ID, 'mcp_servers', load_mcp_config(AGENT_CODE_DIR),
        description='Which MCP servers this agent connects to at startup - editing this needs a restart to take effect.',
    )
    skills = await get_skills()

    return {'host': host, 'port': port, 'description': description, 'mcp_servers': mcp_servers, 'skills': skills}


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Analysis Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=AnalysisAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
