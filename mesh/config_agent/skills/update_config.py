"""
update_config's real body - writes to mesh/lib/config_sdk.py, scoped to
constants only (prompt templates, toggles like analysis.strict_grounding),
not stage hyperparameters (model/temperature/timeout). A stage's structured
{model, temperature, timeout} shape isn't something a free-text WhatsApp
instruction can safely fill in - that's the dashboard's job, a real form,
not fuzzy NL extraction guessing at a temperature value.

Same fuzzy resolution as query_config.py - the caller names an agent/key
approximately, this resolves against what's actually on file.

Two real callers, two different value shapes: WhatsApp's classify/extract
path always hands this a string (an LLM can't reliably produce a typed
value from free text); the config dashboard sends an already-typed JSON
value (bool/int/float/list) straight off a form field. _coerce() below
accepts either - it matches the type of whatever is CURRENTLY stored for
that key (bool/int/float/list/str), not the type of new_value itself, so a
number typed as "0.7" from WhatsApp and a real float 0.7 from the dashboard
both land as the same stored type. Without this, every dashboard save of a
non-string constant (a stage's own numbers aside, but e.g. observation_char_cap,
doc_search_top_k, mcp_servers, skill_*_examples) would have silently
stringified a real int/list into a broken string - see this project's own
recall.py top_k-crossed-the-A2A-boundary-as-a-float bug for why "the type
protobuf/JSON happens to hand you" can't be trusted at either edge.
"""
import json
from typing import Any, Dict, Optional

from mesh.lib import config_sdk
from mesh.config_agent.skills.query_config import fuzzy_match


class _CoerceError(Exception):
    def __init__(self, message: str):
        self.message = message


def _coerce(current: Any, new_value: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(new_value, bool):
            return new_value
        return str(new_value).strip().lower() in ('true', 'yes', 'on', '1', 'enable', 'enabled')

    # bool is a subclass of int in Python - this check must come after the
    # bool branch above, or a real bool would fall into the int branch.
    if isinstance(current, int):
        try:
            return int(new_value)
        except (TypeError, ValueError):
            raise _CoerceError(f'expects a whole number, got {new_value!r}')

    if isinstance(current, float):
        try:
            return float(new_value)
        except (TypeError, ValueError):
            raise _CoerceError(f'expects a number, got {new_value!r}')

    if isinstance(current, list):
        if isinstance(new_value, list):
            return new_value
        if isinstance(new_value, str):
            try:
                parsed = json.loads(new_value)
            except Exception:
                raise _CoerceError(f'expects a list, could not parse {new_value!r} as one')
            if not isinstance(parsed, list):
                raise _CoerceError(f'expects a list, got {new_value!r}')
            return parsed
        raise _CoerceError(f'expects a list, got {new_value!r}')

    return str(new_value)


async def run(agent_id: str, key: str, new_value: Any) -> Dict[str, Any]:
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

    try:
        parsed_value = _coerce(constants[resolved_key], new_value)
    except _CoerceError as e:
        return {'updated': False, 'message': f'{resolved_key!r} {e.message}.'}

    ok = await config_sdk.set_constant(resolved_agent, resolved_key, parsed_value)
    if not ok:
        return {'updated': False, 'message': 'Could not write the update - the config store may be unreachable.'}
    return {'updated': True, 'agent_id': resolved_agent, 'constant': resolved_key, 'new_value': parsed_value}
