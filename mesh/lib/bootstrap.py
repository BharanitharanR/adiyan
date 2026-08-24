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

Two more things every agent gets for free from calling this, both closing
the same real gap: activating a vertical (mesh/lib/config_sdk.py's
set_active_vertical_id()) is supposed to change every agent's behavior at
once, but the Agent Registry only ever learns an agent's skills by fetching
its /.well-known/agent-card.json at registration time - once, at startup -
and never again on its own. Without both pieces below, a vertical's custom
skill description would apply to this agent's own internal classify
decisions (skills_catalog.py's get_skills() already resolves that live) but
never reach what Orchestrator sees when deciding whether to route to this
agent in the first place:
  - skills_refresher (optional, passed to serve()): if given, the served
    agent-card is rebuilt from it on every single fetch, not baked in once
    from the `agent_card` object passed to serve(). AgentCard is a raw
    protobuf message (a2a_pb2.AgentCard, confirmed - no Pydantic
    model_copy()), so rebuilding it means a deepcopy plus ClearField +
    extend() on the repeated `skills` field, not a plain attribute
    reassignment.
  - The vertical-change poller (always runs, regardless of
    skills_refresher): watches config_sdk.get_active_vertical_id() and
    re-registers with the Agent Registry whenever it changes, which is what
    actually makes the registry re-fetch and store the now-current card.
"""
import asyncio
import copy
import logging
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore
from a2a.types import AgentCard, AgentSkill

from mesh.lib import config_sdk, registry_client

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


# How often to check whether the deployment's active vertical changed -
# same cadence as registry_client's own auto-refresh, no reason for this to
# be tighter or looser.
_VERTICAL_POLL_INTERVAL_SECONDS = 30.0

# Sentinel distinct from None (a real, valid "no vertical active" value) -
# guarantees the first poll after startup always compares against
# something it can never equal by coincidence, so a deployment that's
# already running under a vertical at this agent's own startup still gets
# its first re-registration.
_UNCHECKED = object()


def _rebuild_card_with_fresh_skills(agent_card: AgentCard, skills: List[AgentSkill]) -> AgentCard:
    """AgentCard's `skills` is a protobuf repeated field - no plain
    `card.skills = skills` assignment, confirmed live
    (`google._upb._message.RepeatedScalarContainer` has no setter). deepcopy
    first so the original `agent_card` object serve() was called with is
    never mutated out from under it."""
    new_card = copy.deepcopy(agent_card)
    new_card.ClearField('skills')
    new_card.skills.extend(skills)
    return new_card


def _poll_vertical_and_reregister(agent_id: str, url: str) -> None:
    """Runs forever, in its own thread. See this module's own docstring for
    why this exists: the Agent Registry never re-fetches a live agent-card
    on its own, so activating a vertical needs *something* to trigger a
    re-fetch, or Orchestrator's routing decisions keep seeing whatever
    skill description was true at this agent's last startup."""
    last_known_vertical = _UNCHECKED
    while True:
        try:
            current = asyncio.run(config_sdk.get_active_vertical_id())
            if current != last_known_vertical:
                if asyncio.run(registry_client.register(agent_id, url)):
                    logger.info(f"Re-registered {agent_id!r} after active vertical changed to {current!r}")
                    last_known_vertical = current
                # Left unchanged (not re-marked _UNCHECKED) on a failed
                # re-registration - the next poll retries the same
                # transition rather than silently giving up on it.
        except Exception as e:
            logger.warning(f'Vertical-change poll failed for {agent_id!r}: {e}')
        time.sleep(_VERTICAL_POLL_INTERVAL_SECONDS)


def serve(
    agent_card: AgentCard,
    executor: AgentExecutor,
    host: str,
    port: int,
    tasks_db_path: Path,
    agent_id: str,
    skills_refresher: Optional[Callable[[], Awaitable[List[AgentSkill]]]] = None,
) -> None:
    """Blocks, running the agent's A2A server. tasks_db_path should come from
    mesh.lib.paths.tasks_db_path(agent_id) - A2A's own task bookkeeping, kept
    separate from the agent's domain data. agent_id is this agent's own
    registry identity (e.g. 'scheduler') - used only for self-registration,
    see this module's docstring.

    skills_refresher: optional, e.g. a skills_catalog.py's get_skills() -
    if given, this agent's own /.well-known/agent-card.json is rebuilt from
    it on every fetch instead of serving the static `agent_card` object
    forever. Only worth passing once an agent's skills_catalog.py actually
    resolves live config (vertical-aware or not) - an agent whose skills
    never change after startup gets nothing from this beyond one extra
    async call per card fetch, so it stays optional rather than automatic."""
    engine = create_async_engine(f'sqlite+aiosqlite:///{tasks_db_path}')
    task_store = DatabaseTaskStore(engine=engine, create_table=True)

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    card_modifier = None
    if skills_refresher is not None:
        async def card_modifier(card: AgentCard) -> AgentCard:
            return _rebuild_card_with_fresh_skills(card, await skills_refresher())

    routes = []
    routes.extend(create_agent_card_routes(agent_card, card_modifier=card_modifier))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    app = Starlette(routes=routes)

    threading.Thread(
        target=_register_with_retry, args=(agent_id, f'http://{host}:{port}'), daemon=True,
    ).start()

    # Every agent gets a live-ish view of the registry for free, whether or
    # not it happens to call registry_client.get_cached_agents() itself -
    # see registry_client.start_auto_refresh()'s own docstring. Cheap and
    # idempotent even for agents that never read the cache.
    threading.Thread(target=registry_client.start_auto_refresh, daemon=True).start()

    # Runs regardless of skills_refresher - re-registering is harmless
    # (and cheap - registry_client.register() is a single MCP call) even
    # for an agent whose card never actually changes.
    threading.Thread(
        target=_poll_vertical_and_reregister, args=(agent_id, f'http://{host}:{port}'), daemon=True,
    ).start()

    uvicorn.run(app, host=host, port=port)
