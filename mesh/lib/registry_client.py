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
(mesh/AGENTS.md), exactly as before this existed.

Both register() and list_agents() are best-effort, never raise: a
registry that's briefly unreachable (e.g. still starting up) must never
become a crash for whatever's calling this, the same tolerance already
documented for other degraded-but-not-fatal dependencies elsewhere in this
mesh (mesh/memory/memory_index.py's get_memory_index()).
"""
import logging
from typing import Any, Dict, List

from mesh.lib import permissions
from mesh.lib.mcp_client import call_tool

REGISTRY_URL = 'http://127.0.0.1:8424/mcp'

logger = logging.getLogger('RegistryClient')


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
