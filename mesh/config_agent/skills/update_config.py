"""
update_config's real body - writes to mesh/lib/config_sdk.py, scoped to
constants only (prompt templates, toggles like analysis.strict_grounding),
not stage hyperparameters (model/temperature/timeout). A stage's structured
{model, temperature, timeout} shape isn't something a free-text WhatsApp
instruction can safely fill in - that's the dashboard's job, a real form,
not fuzzy NL extraction guessing at a temperature value.

Same fuzzy resolution as query_config.py - the caller names an agent/key
approximately, this resolves against what's actually on file.
"""
from typing import Any, Dict, Optional

from mesh.lib import config_sdk
from mesh.config_agent.skills.query_config import fuzzy_match


async def run(agent_id: str, key: str, new_value: str) -> Dict[str, Any]:
    known_agents = await config_sdk.list_agent_ids()
    resolved_agent = fuzzy_match(agent_id, known_agents)
    if not resolved_agent:
        return {
            'updated': False,
            'message': f"No agent matching {agent_id!r} has configuration on file.",
            'known_agents': known_agents,
        }

    full = await config_sdk.get_full_config(resolved_agent)
    constants = (full or {}).get('constants', {})
    resolved_key = fuzzy_match(key, list(constants.keys())) if constants else None
    if not resolved_key:
        return {
            'updated': False,
            'message': f"No constant matching {key!r} found for {resolved_agent!r} - only "
                       'existing constants can be updated this way, not stage settings '
                       '(model/temperature/timeout) or a brand-new key.',
            'known_constants': list(constants.keys()),
        }

    # A bool-typed constant (e.g. strict_grounding) needs real bool parsing,
    # not the literal string "true"/"false" - everything else stays a string.
    current = constants[resolved_key]
    parsed_value: Any = new_value
    if isinstance(current, bool):
        parsed_value = new_value.strip().lower() in ('true', 'yes', 'on', '1', 'enable', 'enabled')

    ok = await config_sdk.set_constant(resolved_agent, resolved_key, parsed_value)
    if not ok:
        return {'updated': False, 'message': 'Could not write the update - the config store may be unreachable.'}
    return {'updated': True, 'agent_id': resolved_agent, 'constant': resolved_key, 'new_value': parsed_value}
