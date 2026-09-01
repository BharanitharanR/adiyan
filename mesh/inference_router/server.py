"""
Inference Router - A2A server entrypoint. Same shape as every other agent's
server.py (copied from mesh/journal/server.py, the smallest real one).

Run from the repo root as `python -m mesh.inference_router.server`. Does
NOT self-register with mesh.agent_registry (register_with_agent_registry=
False below) - internal platform plumbing reached only via mesh/lib/
agent_sdk.py's ask(), never a valid destination for Orchestrator's
router.py to pick from raw user text. Test directly:

    curl -s http://127.0.0.1:8441/.well-known/agent-card.json

Platform infrastructure (see mesh/lib/agent_sdk.py's ask()), not a
reference shape for other agent authors to copy.
"""
import asyncio
from pathlib import Path

from mesh.inference_router.agent_executor import InferenceRouterExecutor
from mesh.inference_router.constants import AGENT_ID, HOST, PORT
from mesh.inference_router.skills_catalog import get_skills
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
    description = await config_sdk.get_constant(
        AGENT_ID, 'card_description',
        "Runs a one-off LLM completion on behalf of another agent, choosing between this machine's own "
        "Ollama and a peer's spare compute. Internal platform plumbing, not something a human message routes to.",
        description='What this agent does, shown in its A2A agent card.',
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Inference Router',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=InferenceRouterExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
        # Internal platform plumbing, reached only via mesh/lib/agent_sdk.py's
        # ask() (fixed INFERENCE_ROUTER_URL), never a valid destination for
        # Orchestrator's router.py to pick from raw user text. See
        # bootstrap.build_app()'s own docstring.
        register_with_agent_registry=False,
    )
