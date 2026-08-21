"""
The A2A server bootstrap shared by every agent under mesh/ - DatabaseTaskStore,
DefaultRequestHandler, agent-card + JSON-RPC routes, Starlette, uvicorn. Every
agent's own server.py differs only in its AgentCard and AgentExecutor; this
function is the rest.

Also where every agent registers itself with the Agent Registry
(mesh/mcp/agent_registry/) - "any agent that implements the interface
automatically registers itself" turns out to mean "any agent that calls this
shared serve()", so no agent's own server.py needs registration code of its
own. See mesh/lib/registry_client.py for the client side and
mesh/mcp/agent_registry/server.py for why it fetches this agent's own
agent-card back rather than trusting a self-reported skill list.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import AgentCard

from mesh.lib import registry_client

logger = logging.getLogger('bootstrap')

# Self-registration needs this agent's own A2A server to already be
# accepting connections (the registry calls back to this agent's
# /.well-known/agent-card.json to verify it, see agent_registry/server.py) -
# which isn't true yet at the point serve() is called, only shortly after
# uvicorn actually starts listening. Retrying on a background thread with a
# short delay sidesteps needing to reason precisely about ASGI
# lifespan-vs-socket-bind ordering.
_REGISTER_RETRY_ATTEMPTS = 5
_REGISTER_RETRY_DELAY_SECONDS = 1.0


def _register_with_retry(agent_id: str, url: str) -> None:
    for attempt in range(_REGISTER_RETRY_ATTEMPTS):
        time.sleep(_REGISTER_RETRY_DELAY_SECONDS)
        if asyncio.run(registry_client.register(agent_id, url)):
            logger.info(f"Registered with agent registry: {agent_id} -> {url}")
            return
    logger.warning(
        f"Gave up registering {agent_id!r} with the agent registry after "
        f"{_REGISTER_RETRY_ATTEMPTS} attempts - it will not appear in "
        f"Orchestrator's routing pool until this process is restarted."
    )


def serve(
    agent_card: AgentCard,
    executor: AgentExecutor,
    host: str,
    port: int,
    tasks_db_path: Path,
    agent_id: str,
) -> None:
    """Blocks, running the agent's A2A server. tasks_db_path should come from
    mesh.lib.paths.tasks_db_path(agent_id) - A2A's own task bookkeeping, kept
    separate from the agent's domain data. agent_id is this agent's own
    registry identity (e.g. 'scheduler') - used only for self-registration,
    see this module's docstring."""
    engine = create_async_engine(f'sqlite+aiosqlite:///{tasks_db_path}')
    task_store = DatabaseTaskStore(engine=engine, create_table=True)

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    app = Starlette(routes=routes)

    threading.Thread(
        target=_register_with_retry, args=(agent_id, f'http://{host}:{port}'), daemon=True,
    ).start()

    uvicorn.run(app, host=host, port=port)
