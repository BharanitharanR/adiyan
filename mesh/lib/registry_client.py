"""
Shared client for the Agent Registry MCP server (mesh/mcp/agent_registry/).

Every real A2A agent under mesh/ registers itself here once at startup - via
mesh/lib/bootstrap.py's serve(), not by each agent writing its own
registration code. That's the actual mechanism behind "any agent that
implements the interface automatically registers itself": using the shared
bootstrap already every agent's server.py calls IS implementing the
interface. No per-agent boilerplate needed.

Only real A2A agents register here, never MCP-only servers (cron_trigger,
whatsapp) - the registry works by fetching an agent's own
/.well-known/agent-card.json, which only A2A agents serve. An MCP server has
tools, not AgentSkills, and stays discovered by direct URL knowledge
(docs/AGENTS.md), exactly as before this existed.

Both register() and list_agents() are best-effort, never raise: a
registry that's briefly unreachable (e.g. still starting up) must never
become a crash for whatever's calling this, the same tolerance already
documented for other degraded-but-not-fatal dependencies elsewhere in this
mesh (mesh/memory/memory_index.py's get_memory_index()).

start_auto_refresh()/get_cached_agents() are the fix for a real bug: a
caller that snapshots list_agents() once at its own startup (Orchestrator's
router.py used to) can permanently miss an agent that registers later (e.g.
Analysis Agent, whose heavier LangChain imports make it register after
Orchestrator's own load window already closed) - confirmed live, more than
once. A caller that wants a live-ish view without paying a network round
trip on every single use should call start_auto_refresh() once at its own
startup (bootstrap.py's serve() already does, so every agent gets this for
free) and read get_cached_agents() from then on.
"""
import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from mesh.lib import permissions
from mesh.lib.mcp_client import call_tool

REGISTRY_URL = 'http://127.0.0.1:8424/mcp'

# Configurable via AGENT_REGISTRY_REFRESH_SECONDS - how often
# start_auto_refresh()'s background thread re-fetches the registry.
DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0

# First-fetch retry knobs for start_auto_refresh()'s initial, blocking
# populate - same 40 x 1.5s = 60s cold-start ceiling router.py's old
# load_agent_pool() used, now living here instead.
_INITIAL_RETRY_ATTEMPTS = 40
_INITIAL_RETRY_DELAY_SECONDS = 1.5

logger = logging.getLogger('RegistryClient')

_cache_lock = threading.Lock()
_cached_agents: List[Dict[str, Any]] = []
_refresh_started = False


async def register(agent_id: str, url: str) -> bool:
    """True on success, False otherwise. Callers that need to retry (e.g.
    bootstrap.py's self-registration, which can only succeed once this
    agent's own A2A server is actually accepting connections) check the
    return value themselves - this function does not retry internally."""
    token = permissions.mint_token('service', 'service')
    try:
        await call_tool(REGISTRY_URL, 'register_agent', {'agent_id': agent_id, 'url': url}, token=token)
        return True
    except Exception as e:
        logger.debug(f"Registration attempt failed for {agent_id!r}: {e}")
        return False


async def list_agents() -> List[Dict[str, Any]]:
    """Whatever the registry currently knows - each entry shaped
    {'agent_id', 'url', 'skills'}. Empty list if nothing has registered yet,
    or if the registry itself is unreachable - never an exception."""
    token = permissions.mint_token('service', 'service')
    try:
        result = await call_tool(REGISTRY_URL, 'list_agents', {}, token=token)
        return result.get('agents', [])
    except Exception as e:
        logger.warning(f"Could not reach agent registry for list_agents: {e}")
        return []


def _refresh_loop(interval_seconds: float) -> None:
    global _cached_agents
    while True:
        agents = asyncio.run(list_agents())
        if agents:
            with _cache_lock:
                _cached_agents = agents
        time.sleep(interval_seconds)


def start_auto_refresh(interval_seconds: Optional[float] = None) -> None:
    """Idempotent - safe to call from every agent's own startup (bootstrap.py
    does, so no per-agent wiring is needed) and safe to call again elsewhere
    (e.g. router.load_agent_pool() also calls this directly, since it needs
    the pool populated before bootstrap.serve() even runs - the second call
    is just a no-op).

    Blocks on the first fetch, retrying up to _INITIAL_RETRY_ATTEMPTS times
    (60s ceiling) so a caller that needs agents to already be discoverable
    right after this returns (Orchestrator routing real traffic) doesn't
    start against a known-empty cache. After that, a daemon thread re-fetches
    every interval_seconds (env var AGENT_REGISTRY_REFRESH_SECONDS, default
    30) for as long as the process runs. A refresh that returns nothing
    (registry briefly unreachable) leaves the last-known-good cache in place
    rather than clearing it - a transient blip shouldn't make every agent
    briefly unroutable."""
    global _refresh_started
    with _cache_lock:
        if _refresh_started:
            return
        _refresh_started = True

    resolved_interval = interval_seconds if interval_seconds is not None else float(
        os.environ.get('AGENT_REGISTRY_REFRESH_SECONDS', str(DEFAULT_REFRESH_INTERVAL_SECONDS))
    )

    global _cached_agents
    for attempt in range(_INITIAL_RETRY_ATTEMPTS):
        agents = asyncio.run(list_agents())
        if agents:
            with _cache_lock:
                _cached_agents = agents
            logger.info(f"Agent registry cache populated: {len(agents)} agent(s)")
            break
        time.sleep(_INITIAL_RETRY_DELAY_SECONDS)
    else:
        logger.warning(
            "Agent registry returned no agents after retrying - starting "
            "with an empty cache, the background refresh will pick up "
            "anything that registers later."
        )

    threading.Thread(target=_refresh_loop, args=(resolved_interval,), daemon=True).start()


def get_cached_agents() -> List[Dict[str, Any]]:
    """The last snapshot start_auto_refresh()'s background thread fetched -
    no network call. Empty if start_auto_refresh() was never called, or if
    its initial fetch never found anything (see its own docstring)."""
    with _cache_lock:
        return list(_cached_agents)
