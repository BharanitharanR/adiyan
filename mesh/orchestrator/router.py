"""
Coarse-grained routing: which known agent should handle this message?
Reuses skill_router.classify() unchanged, pooled across whichever agents
the Agent Registry (mesh/mcp/agent_registry/) currently knows about -
replaces the old hardcoded _AGENT_SKILLS import list, which needed a manual
edit here every time an agent's skill catalog changed.

Ported from mesh/whatsapp_connector/router.py (now retired) - same logic,
now living in the component that actually owns the routing decision.

Deliberately stops at "which agent" - does NOT also decide which skill or
extract parameters. That's the target agent's own classify_skill/
extract_parameters job once the raw text is forwarded to it.

The pool is loaded once, at Orchestrator's own startup (load_agent_pool()),
not re-fetched per message - a live registry round-trip on every single
message is exactly the class of added latency that already caused real
problems elsewhere in this mesh (the webhook-redelivery bug traced back to
Orchestrator being too slow to respond). An agent that registers after
Orchestrator has already loaded its pool won't be routable to until
Orchestrator restarts - the same "restart is the recovery/refresh model"
already accepted everywhere else in this mesh.
"""
import asyncio
import logging
import time
from typing import Any, Dict, Optional

from a2a.types import AgentSkill

from mesh.lib import registry_client
from mesh.lib.skill_router import classify

logger = logging.getLogger('Router')

# See load_agent_pool()'s docstring - other agents may not have finished
# registering yet at the exact moment Orchestrator starts. Confirmed live:
# with agent_registry itself up, Scheduler/Journal/Memory still took ~9-13s
# to import their dependencies and reach their own first registration
# attempt - a short retry window (originally 5s total) reliably lost this
# race and left Orchestrator permanently routing against an empty pool.
# 40 attempts x 1.5s = 60s ceiling comfortably covers a cold start.
_LOAD_RETRY_ATTEMPTS = 40
_LOAD_RETRY_DELAY_SECONDS = 1.5

# skill_id -> agent's base URL
_url_by_skill_id: Dict[str, str] = {}
# skill_id -> real AgentSkill, reconstructed from the registry's plain-dict
# data - classify()'s prompt-building needs actual AgentSkill objects
# (attribute access: skill.id, skill.description, skill.examples).
_skills_by_id: Dict[str, AgentSkill] = {}
# agent_id -> base URL - for a caller that already knows exactly which
# agent and skill it wants (e.g. handle_message.py's kb_pending branch
# calling Memory Agent's ingest_document directly), same reasoning
# call_agent() itself already documents: no NLU needed when the caller
# already knows. Deliberately separate from _url_by_skill_id - a skill a
# caller only ever reaches this way (never usefully free-text classified,
# since it needs real binary content no prose can supply) has no reason to
# be in the classify pool at all, so it's not in _skills_by_id/SKILLS either.
_url_by_agent_id: Dict[str, str] = {}


def load_agent_pool() -> None:
    """Blocking - call once from orchestrator/server.py before serve()
    starts serving. Retries a few times with a short delay, since other
    agents' own self-registration (mesh/lib/bootstrap.py) is itself
    retrying in the background at the same time Orchestrator is starting."""
    global _url_by_skill_id, _skills_by_id, _url_by_agent_id
    for attempt in range(_LOAD_RETRY_ATTEMPTS):
        agents = asyncio.run(registry_client.list_agents())
        if agents:
            url_by_skill_id: Dict[str, str] = {}
            skills_by_id: Dict[str, AgentSkill] = {}
            url_by_agent_id: Dict[str, str] = {}
            for entry in agents:
                url_by_agent_id[entry['agent_id']] = entry['url']
                for skill in entry.get('skills', []):
                    skill_id = skill['id']
                    url_by_skill_id[skill_id] = entry['url']
                    skills_by_id[skill_id] = AgentSkill(**skill)
            _url_by_skill_id = url_by_skill_id
            _skills_by_id = skills_by_id
            _url_by_agent_id = url_by_agent_id
            logger.info(f"Loaded agent pool: {len(agents)} agent(s), {len(skills_by_id)} skill(s)")
            return
        time.sleep(_LOAD_RETRY_DELAY_SECONDS)
    logger.warning(
        "Agent registry returned no agents after retrying - routing will "
        "find no matches until this process is restarted."
    )


def get_agent_url(agent_id: str) -> Optional[str]:
    """None if agent_id isn't (yet) in the pool - same staleness caveat as
    the rest of this module: only refreshed at Orchestrator's own startup."""
    return _url_by_agent_id.get(agent_id)


async def route_to_agent(text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Returns the target agent's base URL, or None if nothing matches."""
    if not _skills_by_id:
        return None
    choice = await classify(text, list(_skills_by_id.values()), cfg)
    if choice.skill_id is None:
        return None
    return _url_by_skill_id[choice.skill_id]
