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
import contextlib
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


async def _poll_vertical_and_reregister(agent_id: str, url: str) -> None:
    """Runs forever, as an asyncio task on the SAME event loop uvicorn
    itself runs on - not a separate thread with its own asyncio.run() loop.

    Confirmed live this distinction actually matters: config_sdk.py caches
    a single AsyncMongoClient, bound to whichever event loop first used it
    - unlike registry_client.py's calls (a fresh, stateless MCP session per
    call, safe from any throwaway asyncio.run() loop), config_sdk has real
    state tied to one specific loop. A background thread calling
    asyncio.run(config_sdk...) in a loop creates a brand new event loop
    every cycle, which broke every OTHER caller's config_sdk reads mesh-wide
    the moment it touched the client first - 'Cannot use AsyncMongoClient
    in different event loop' errors on completely unrelated calls, not just
    this poller's own. Scheduled via Starlette's on_startup (see serve()
    below), not threading.Thread, so it shares uvicorn's own loop instead
    of competing with it.

    See this module's own top docstring for why this exists at all: the
    Agent Registry never re-fetches a live agent-card on its own, so
    activating a vertical needs *something* to trigger a re-fetch, or
    Orchestrator's routing decisions keep seeing whatever skill description
    was true at this agent's last startup."""
    last_known_vertical = _UNCHECKED
    while True:
        try:
            current = await config_sdk.get_active_vertical_id()
            if current != last_known_vertical:
                if await registry_client.register(agent_id, url):
                    logger.info(f"Re-registered {agent_id!r} after active vertical changed to {current!r}")
                    last_known_vertical = current
                # Left unchanged (not re-marked _UNCHECKED) on a failed
                # re-registration - the next poll retries the same
                # transition rather than silently giving up on it.
        except Exception as e:
            logger.warning(f'Vertical-change poll failed for {agent_id!r}: {e}')
        await asyncio.sleep(_VERTICAL_POLL_INTERVAL_SECONDS)


def build_app(
    agent_card: AgentCard,
    executor: AgentExecutor,
    host: str,
    port: int,
    tasks_db_path: Path,
    agent_id: str,
    skills_refresher: Optional[Callable[[], Awaitable[List[AgentSkill]]]] = None,
) -> Starlette:
    """Everything serve() needs before starting the two background threads
    and calling uvicorn.run() - split into its own function so
    mesh/tools/smoke_test.py can build a real app object for every agent
    and drive it through Starlette's own TestClient (which triggers a real
    ASGI lifespan startup/shutdown) without binding a real port, making a
    real network call, or blocking forever.

    This split exists because of a real incident: 'import the module'
    checks never once touched this code (it only ever ran inside
    `if __name__ == '__main__':`), so a broken Starlette() call here took
    down every agent simultaneously before anyone found out. See
    mesh/tools/smoke_test.py's own docstring.

    tasks_db_path should come from mesh.lib.paths.tasks_db_path(agent_id) -
    A2A's own task bookkeeping, kept separate from the agent's domain data.
    agent_id is this agent's own registry identity (e.g. 'scheduler') -
    used for self-registration and the vertical poller, see this module's
    docstring.

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

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette):
        # The installed Starlette (1.6.0) dropped on_startup/on_shutdown
        # entirely in favor of this ASGI lifespan protocol - confirmed live
        # (TypeError: unexpected keyword argument 'on_startup') before
        # switching to it. asyncio.create_task here, not threading.Thread -
        # see _poll_vertical_and_reregister()'s own docstring for why this
        # one specifically has to share uvicorn's own event loop rather
        # than get a thread of its own like the two started in serve()
        # below. Runs regardless of skills_refresher - re-registering is
        # harmless (and cheap - a single MCP call) even for an agent whose
        # card never changes.
        task = asyncio.create_task(_poll_vertical_and_reregister(agent_id, f'http://{host}:{port}'))
        yield
        task.cancel()

    routes = []
    routes.extend(create_agent_card_routes(agent_card, card_modifier=card_modifier))
    routes.extend(create_jsonrpc_routes(request_handler, '/'))

    return Starlette(routes=routes, lifespan=_lifespan)


def serve(
    agent_card: AgentCard,
    executor: AgentExecutor,
    host: str,
    port: int,
    tasks_db_path: Path,
    agent_id: str,
    skills_refresher: Optional[Callable[[], Awaitable[List[AgentSkill]]]] = None,
) -> None:
    """Blocks, running the agent's A2A server. See build_app()'s own
    docstring for what it builds and why that part is split out; this
    function is just that plus the two background threads (real side
    effects - a registry registration, a periodic MCP poll - deliberately
    not run by the smoke test) and the actual uvicorn.run() call."""
    app = build_app(agent_card, executor, host, port, tasks_db_path, agent_id, skills_refresher)

    threading.Thread(
        target=_register_with_retry, args=(agent_id, f'http://{host}:{port}'), daemon=True,
    ).start()

    # Every agent gets a live-ish view of the registry for free, whether or
    # not it happens to call registry_client.get_cached_agents() itself -
    # see registry_client.start_auto_refresh()'s own docstring. Cheap and
    # idempotent even for agents that never read the cache. Safe as its own
    # thread (unlike the vertical poller above) - list_agents()/register()
    # are stateless per-call MCP sessions, no cached client tied to a
    # specific event loop.
    threading.Thread(target=registry_client.start_auto_refresh, daemon=True).start()

    uvicorn.run(app, host=host, port=port)
