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

The pool used to be loaded once, at Orchestrator's own startup, and never
refreshed after that - an agent that registered after Orchestrator's load
window closed (Analysis Agent's heavier LangChain imports made this happen
live, more than once) was permanently unroutable until Orchestrator itself
restarted. Now derived on every call from registry_client's own
auto-refreshing cache (registry_client.start_auto_refresh(), interval
configurable via AGENT_REGISTRY_REFRESH_SECONDS, default 30s) instead of a
private one-time snapshot - rebuilding a handful of dicts from an
already-cached list is cheap, so there's no reason to also cache the
derived form here.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from a2a.types import AgentSkill

from mesh.lib import registry_client
from mesh.lib.skill_router import classify

logger = logging.getLogger('Router')


def load_agent_pool() -> None:
    """Blocking - call once from orchestrator/server.py before serve()
    starts serving, so Orchestrator doesn't start accepting real traffic
    against a known-empty registry cache. See
    registry_client.start_auto_refresh()'s own docstring for the retry/
    refresh behavior - this is now just that."""
    registry_client.start_auto_refresh()


def _build_pool() -> Tuple[Dict[str, str], Dict[str, AgentSkill], Dict[str, str]]:
    """(url_by_skill_id, skills_by_id, url_by_agent_id), rebuilt from
    registry_client.get_cached_agents()'s current snapshot - see this
    module's own docstring on why this isn't cached here too."""
    url_by_skill_id: Dict[str, str] = {}
    skills_by_id: Dict[str, AgentSkill] = {}
    url_by_agent_id: Dict[str, str] = {}
    for entry in registry_client.get_cached_agents():
        url_by_agent_id[entry['agent_id']] = entry['url']
        for skill in entry.get('skills', []):
            skill_id = skill['id']
            url_by_skill_id[skill_id] = entry['url']
            skills_by_id[skill_id] = AgentSkill(**skill)
    return url_by_skill_id, skills_by_id, url_by_agent_id


def get_agent_url(agent_id: str) -> Optional[str]:
    """None if agent_id isn't in the registry's current cache - refreshed
    at most AGENT_REGISTRY_REFRESH_SECONDS seconds ago, not only at
    Orchestrator's own startup."""
    _, _, url_by_agent_id = _build_pool()
    return url_by_agent_id.get(agent_id)


async def route_to_agent(text: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Returns the target agent's base URL, or None if nothing matches."""
    url_by_skill_id, skills_by_id, _ = _build_pool()
    if not skills_by_id:
        return None
    choice = await classify(text, list(skills_by_id.values()), cfg)
    if choice.skill_id is None:
        return None
    return url_by_skill_id[choice.skill_id]
