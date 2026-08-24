"""
query_config's real body - read-only lookup against mesh/lib/config_sdk.py,
resolved fuzzily against whatever real agent_ids/keys actually exist rather
than trusting the caller's (or the extraction LLM's) exact spelling. The
config vocabulary (agent_ids, stage names, constant keys) is project-
specific and not something a classify/extract prompt can be expected to
get letter-perfect from a WhatsApp message alone.
"""
from typing import Any, Dict, Optional

from mesh.lib import config_sdk


def _normalize(text: str) -> str:
    """Stored keys are snake_case ('strict_grounding'); a WhatsApp message
    naturally phrases the same thing with spaces ('strict grounding'). Both
    the exact-match and substring checks below need spaces and underscores
    treated as the same separator, or a perfectly natural phrasing of a
    real, existing key never matches - confirmed live: "turn on strict
    grounding for Analysis Agent" failed with "not an existing constant"
    even though analysis.strict_grounding was already on file, because
    'strict grounding' is neither equal to nor a substring of
    'strict_grounding' without this normalization."""
    return text.lower().replace('_', ' ').replace('-', ' ')


def fuzzy_match(guess: Optional[str], candidates: list) -> Optional[str]:
    """Case-insensitive substring match, either direction, with spaces and
    underscores treated as equivalent - None if no candidate matches, the
    guess itself if unset (caller wants everything)."""
    if not guess:
        return None
    guess_norm = _normalize(guess)
    for candidate in candidates:
        if guess_norm == _normalize(candidate):
            return candidate
    for candidate in candidates:
        candidate_norm = _normalize(candidate)
        if guess_norm in candidate_norm or candidate_norm in guess_norm:
            return candidate
    return None


async def run(agent_id: Optional[str] = None, key: Optional[str] = None) -> Dict[str, Any]:
    known_agents = await config_sdk.list_agent_ids()
    if not known_agents:
        return {'found': False, 'message': 'No agent has any configuration on file yet.'}

    resolved_agent = fuzzy_match(agent_id, known_agents)
    if agent_id and not resolved_agent:
        return {
            'found': False,
            'message': f"No agent matching {agent_id!r} has configuration on file.",
            'known_agents': known_agents,
        }
    if not resolved_agent:
        return {'found': False, 'message': 'Which agent? Known agents with configuration: ' + ', '.join(known_agents)}

    full = await config_sdk.get_full_config(resolved_agent)
    if full is None:
        return {'found': False, 'message': f"{resolved_agent!r} has no configuration on file."}

    if not key:
        return {'found': True, 'agent_id': resolved_agent, 'stages': full['stages'], 'constants': full['constants']}

    all_keys = list(full['stages'].keys()) + list(full['constants'].keys())
    resolved_key = fuzzy_match(key, all_keys)
    if not resolved_key:
        return {
            'found': False,
            'message': f"No key matching {key!r} found for {resolved_agent!r}.",
            'known_keys': all_keys,
        }
    if resolved_key in full['stages']:
        return {'found': True, 'agent_id': resolved_agent, 'stage': resolved_key, 'value': full['stages'][resolved_key]}
    return {'found': True, 'agent_id': resolved_agent, 'constant': resolved_key, 'value': full['constants'][resolved_key]}
