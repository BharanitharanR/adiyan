"""
agent_registry - an MCP server, not an A2A agent (same category as
cron_trigger: pure bookkeeping, no AgentCard, no reasoning of its own). Holds
an in-memory directory of {agent_id: {url, skills}}, built from whichever A2A
agents have called register_agent since this process started.

This is the actual "any agent that implements the interface automatically
registers itself" mechanism: the interface IS mesh/lib/bootstrap.py's
serve(), which every agent already calls, and which now registers with this
server as part of starting up. No per-agent registration code, and no
per-agent import list in the router to keep in sync by hand - see
mesh/orchestrator/router.py, the consumer side of this.

Deliberately does not trust a registering agent's self-reported skill list -
on every register_agent call, this server itself fetches the caller's own
/.well-known/agent-card.json (the same a2a-sdk mechanism mesh/lib/a2a_client.py
already uses for every direct agent-to-agent call) and stores that as the
authoritative skill data. A registration payload is just "I exist, here's my
URL" - the skills come from the source of truth, not from what the caller
claims. This also means register_agent fails if url isn't actually serving
yet, which is why bootstrap.py's self-registration retries with a short
delay rather than registering before its own server can answer that fetch.

In-memory only, no persistence - confirmed acceptable: an agent_registry
restart means every agent re-registers once it's back up, the same
"restart is the recovery model" already accepted for every other component
in this mesh, not a new tradeoff invented here.

Must run before any other agent - mesh/start_all.sh starts it first - since
registration only succeeds once this is already up.

Run from the repo root as `python -m mesh.mcp.agent_registry.server`.
"""
import asyncio
import logging
from typing import Any, Dict, List

import httpx
from a2a.client import A2ACardResolver
from mcp.server.fastmcp import Context, FastMCP

from mesh.lib import permissions
from mesh.mcp.agent_registry.constants import HOST, PORT, SERVER_NAME

logger = logging.getLogger(SERVER_NAME)

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)

# agent_id -> {'url': str, 'skills': [{'id', 'name', 'description', 'tags',
# 'examples', 'input_modes', 'output_modes'}, ...]}
_directory: Dict[str, Dict[str, Any]] = {}


async def _fetch_skills(url: str) -> List[Dict[str, Any]]:
    """Fetches the registering agent's own agent-card and returns its skills
    as plain dicts, carrying every AgentSkill field - a faithful round-trip,
    so a consumer (router.py) can reconstruct real AgentSkill objects from
    this later without missing fields. Raises on failure; register_agent
    below decides what that means for the registration itself."""
    async with httpx.AsyncClient(timeout=10) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=url)
        card = await resolver.get_agent_card()
    return [
        {
            'id': s.id,
            'name': s.name,
            'description': s.description,
            # list(...): A2A's AgentSkill is protobuf-backed (see server.py's
            # own module docstring on targeting A2A v1.0's protobuf
            # definition) - these fields are RepeatedScalarContainer, not a
            # plain list, and FastMCP's tool-return serializer can't
            # JSON-encode that. Confirmed live: every list_agents call failed
            # with "Unable to serialize unknown type:
            # google._upb._message.RepeatedScalarContainer" until this
            # explicit conversion was added.
            'tags': list(s.tags),
            'examples': list(s.examples),
            'input_modes': list(s.input_modes),
            'output_modes': list(s.output_modes),
        }
        for s in (card.skills or [])
    ]


@mcp.tool()
async def register_agent(agent_id: str, url: str, ctx: Context) -> Dict[str, Any]:
    """Registers (or re-registers) agent_id at url. Fetches url's own
    agent-card to get its real skill list - see this module's docstring for
    why that isn't trusted from the caller instead. Raises if the card can't
    be fetched (e.g. the registering agent's own A2A server isn't actually
    reachable yet) - a registration claiming a URL that doesn't work is
    worse than no registration at all."""
    permissions.enforce_mcp_permission(ctx, 'mcp.agent_registry.register_agent')
    skills = await _fetch_skills(url)
    _directory[agent_id] = {'url': url, 'skills': skills}
    logger.info(f"Registered {agent_id!r} at {url} ({len(skills)} skill(s))")
    return {'registered': True, 'agent_id': agent_id, 'skill_count': len(skills)}


@mcp.tool()
def list_agents(ctx: Context) -> Dict[str, Any]:
    """Returns every currently-registered agent - id, url, and skills."""
    permissions.enforce_mcp_permission(ctx, 'mcp.agent_registry.list_agents')
    agents = [{'agent_id': aid, **data} for aid, data in _directory.items()]
    return {'agents': agents}


async def main() -> None:
    await mcp.run_streamable_http_async()


if __name__ == '__main__':
    asyncio.run(main())
